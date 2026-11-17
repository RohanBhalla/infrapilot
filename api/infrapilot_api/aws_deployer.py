"""The deployer on AWS (decision 004): two Step Functions workflows do the work, and they report
back through the sandbox ops Lambda (sandbox_handler.py), so `start` and `teardown` return None.

- Deploy: CreateStack under the sandbox service role, check every 15 s, upload the sample files,
  record `deployed` with the stack outputs. A failed stack is deleted by CloudFormation itself
  (OnFailure=DELETE) and recorded `failed`.
- Teardown: empty the stack's buckets, DeleteStack, wait until it's gone, record `torn_down`.

The bundle is saved to `requests/<id>/bundle.json` in the artifacts bucket (what was deployed,
and where the ops Lambda finds the files to upload). The live log is the stack's events.
"""

from __future__ import annotations

import json
import time

from infrapilot_assistant.sandbox import Bundle

from .deploy_requests import DeployRequest
from .deploys import Logs, StackEvent


def bundle_key(request_id: str) -> str:
    return f"requests/{request_id}/bundle.json"


class StepFunctionsDeployer:
    def __init__(self, deploy_machine: str, teardown_machine: str, bucket: str, clients=None):
        if clients is None:
            import boto3

            clients = {n: boto3.client(n) for n in ("stepfunctions", "s3", "cloudformation")}
        self.deploy_machine, self.teardown_machine, self.bucket = deploy_machine, teardown_machine, bucket
        self.sfn, self.s3, self.cfn = clients["stepfunctions"], clients["s3"], clients["cloudformation"]

    def start(self, req: DeployRequest, bundle: Bundle) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=bundle_key(req.id), Body=bundle.model_dump_json().encode(),
                           ContentType="application/json")
        self.sfn.start_execution(stateMachineArn=self.deploy_machine, name=req.id, input=json.dumps({
            "request_id": req.id,
            "stack_name": bundle.stack_name,
            "template_body": bundle.template_body(),
            "parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in bundle.parameters.items()],
        }))
        return None

    def teardown(self, req: DeployRequest) -> None:
        # A new name per attempt: a teardown that failed can be tried again.
        self.sfn.start_execution(stateMachineArn=self.teardown_machine, name=f"{req.id}-{int(time.time())}",
                                 input=json.dumps({"request_id": req.id, "stack": req.stack_id or req.stack_name}))
        return None

    def logs(self, req: DeployRequest, after: str | None) -> Logs:
        """Stack events newer than `after`, oldest first. Events come newest first, so read pages
        until we reach the last one already shown (a deploy has a few dozen at most)."""
        events, kwargs = [], {"StackName": req.stack_id or req.stack_name}
        try:
            while True:
                page = self.cfn.describe_stack_events(**kwargs)
                for e in page["StackEvents"]:
                    if e["EventId"] == after:
                        break
                    events.append(e)
                else:
                    if page.get("NextToken"):
                        kwargs["NextToken"] = page["NextToken"]
                        continue
                break
        except self.cfn.exceptions.ClientError as exc:
            if "does not exist" in str(exc):  # not created yet
                return Logs(next=after)
            raise
        events.reverse()
        out = [StackEvent(id=e["EventId"], at=e["Timestamp"].isoformat(), resource=e["LogicalResourceId"],
                          status=e["ResourceStatus"], reason=e.get("ResourceStatusReason")) for e in events]
        return Logs(events=out, next=out[-1].id if out else after)
