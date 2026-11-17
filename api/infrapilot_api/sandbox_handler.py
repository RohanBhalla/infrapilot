"""The sandbox ops Lambda: the small jobs the deploy and teardown workflows can't do with a plain
AWS call (infra/infrapilot_infra/deploy_workflow.py). Each event is `{"action": ..., ...}`:

- `status` {stack}: the stack's status, or GONE once it's deleted
- `started` {request_id, stack_id}: remember the stack id (its events stay readable after delete)
- `deployed` {request_id, stack}: upload the bundle's sample files, record the stack outputs
- `failed` {request_id, stack, error?}: record why (the first failed resource), and whether
  CloudFormation already deleted the stack
- `empty` {stack}: delete every object in the stack's buckets, so DeleteStack can remove them
- `torn_down` {request_id}, `teardown_failed` {request_id, error?}

Environment: REQUESTS_TABLE, ARTIFACTS_BUCKET.
"""

from __future__ import annotations

import mimetypes
import os

from aws_lambda_powertools import Logger

from infrapilot_assistant.sandbox import STACK_PREFIX, Bundle

from .aws_deployer import bundle_key
from .deploy_requests import DynamoRequestStore
from .deploys import DeployService, Outcome

logger = Logger(service="infrapilot-sandbox")

_clients: dict = {}


def client(name: str):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def _service() -> DeployService:
    if "service" not in _clients:
        _clients["service"] = DeployService(DynamoRequestStore(os.environ["REQUESTS_TABLE"]), None, None)
    return _clients["service"]


def _stack(stack: str) -> dict | None:
    try:
        found = client("cloudformation").describe_stacks(StackName=stack)["Stacks"]
    except client("cloudformation").exceptions.ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    return found[0] if found else None


def status(event) -> dict:
    s = _stack(event["stack"])
    gone = s is None or s["StackStatus"] == "DELETE_COMPLETE"
    return {"status": "GONE" if gone else s["StackStatus"]}


def started(event) -> dict:
    _service().requests.transition(event["request_id"], {"deploying"}, stack_id=event["stack_id"])
    return {}


def deployed(event) -> dict:
    s = _stack(event["stack"]) or {}
    outputs = {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}
    body = client("s3").get_object(Bucket=os.environ["ARTIFACTS_BUCKET"], Key=bundle_key(event["request_id"]))
    bundle = Bundle.model_validate_json(body["Body"].read())
    if bundle.uploads and bundle.upload_to:
        bucket = outputs[bundle.upload_to]
        if not bucket.startswith(STACK_PREFIX):  # the boundary would refuse it anyway
            raise ValueError(f"upload bucket {bucket!r} isn't a sandbox bucket")
        for key, content in bundle.uploads.items():
            client("s3").put_object(Bucket=bucket, Key=key, Body=content.encode(),
                                    ContentType=mimetypes.guess_type(key)[0] or "application/octet-stream")
    _service().record(event["request_id"], Outcome(status="deployed", outputs=outputs))
    return {"outputs": outputs}


def _failure_reason(stack: str) -> str | None:
    """The first resource that failed, oldest first, skipping the ones cancelled because of it."""
    cfn, events, kwargs = client("cloudformation"), [], {"StackName": stack}
    try:
        while True:  # every page: a failed create plus its delete can be more than one
            page = cfn.describe_stack_events(**kwargs)
            events += page["StackEvents"]
            if not page.get("NextToken"):
                break
            kwargs["NextToken"] = page["NextToken"]
    except cfn.exceptions.ClientError:
        return None
    for e in reversed(events):
        reason = e.get("ResourceStatusReason") or ""
        if e["ResourceStatus"].endswith("_FAILED") and "cancelled" not in reason.lower():
            return f"{e['LogicalResourceId']}: {reason}"[:500]
    return None


def failed(event) -> dict:
    stack = event.get("stack")
    reason = (_failure_reason(stack) if stack else None) or event.get("error") or "The deploy failed."
    gone = stack is None or status({"stack": stack})["status"] == "GONE"
    _service().record(event["request_id"], Outcome(status="failed", error=reason, stack_gone=gone))
    return {"error": reason}


def empty(event) -> dict:
    s3, emptied = client("s3"), []
    try:
        pages = client("cloudformation").get_paginator("list_stack_resources").paginate(StackName=event["stack"])
        buckets = [r["PhysicalResourceId"] for page in pages for r in page["StackResourceSummaries"]
                   if r["ResourceType"] == "AWS::S3::Bucket" and r.get("PhysicalResourceId", "").startswith(STACK_PREFIX)
                   and r["ResourceStatus"] != "DELETE_COMPLETE"]
    except client("cloudformation").exceptions.ClientError as exc:
        if "does not exist" in str(exc):
            return {"emptied": []}
        raise
    for bucket in buckets:
        try:
            for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
                keys = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                        for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
                if keys:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        except s3.exceptions.NoSuchBucket:
            continue
        emptied.append(bucket)
    return {"emptied": emptied}


def torn_down(event) -> dict:
    _service().record(event["request_id"], Outcome(status="torn_down"))
    return {}


def teardown_failed(event) -> dict:
    _service().record(event["request_id"], Outcome(status="failed", error=event.get("error") or
                                                   "The sandbox couldn't be deleted; it will be retried."))
    return {}


ACTIONS = {f.__name__: f for f in (status, started, deployed, failed, empty, torn_down, teardown_failed)}


@logger.inject_lambda_context(clear_state=True)
def handler(event, context):
    logger.append_keys(action=event.get("action"), request_id=event.get("request_id"))
    return ACTIONS[event["action"]](event)
