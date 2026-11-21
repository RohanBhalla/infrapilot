"""The AWS deployer and the sandbox ops Lambda, with moto for DynamoDB/S3 and a fake CloudFormation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from infrapilot_api.aws_deployer import StepFunctionsDeployer, bundle_key
from infrapilot_api.deploy_requests import DynamoRequestStore
from infrapilot_assistant.sandbox import Bundle

from test_deploys import new, table  # noqa: F401  (fixture)


def missing(stack):
    return ClientError({"Error": {"Code": "ValidationError", "Message": f"Stack with id {stack} does not exist"}},
                       "DescribeStacks")


class FakeCfn:
    class exceptions:
        ClientError = ClientError

    def __init__(self, stacks=None, events=(), resources=(), summaries=()):
        self.stacks, self.events, self.resources = stacks or {}, list(events), list(resources)
        self.summaries, self.deleted = list(summaries), []  # list_stacks, delete_stack

    def delete_stack(self, StackName, RoleARN):
        self.deleted.append((StackName, RoleARN))

    def describe_stacks(self, StackName):
        if StackName not in self.stacks:
            raise missing(StackName)
        return {"Stacks": [self.stacks[StackName]]}

    def describe_stack_events(self, StackName, NextToken=None):
        if StackName not in self.stacks:
            raise missing(StackName)
        start = int(NextToken or 0)  # pages of 2, newest first
        page = {"StackEvents": self.events[start:start + 2]}
        if start + 2 < len(self.events):
            page["NextToken"] = str(start + 2)
        return page

    def get_paginator(self, name):
        cfn = self

        class P:
            def paginate(self, StackName=None):
                if name == "list_stacks":
                    return [{"StackSummaries": cfn.summaries}]
                if StackName not in cfn.stacks:
                    raise missing(StackName)
                return [{"StackResourceSummaries": cfn.resources}]

        return P()


def event(n, resource="SiteBucket", status="CREATE_COMPLETE", reason=None):
    e = {"EventId": f"e{n}", "Timestamp": datetime(2026, 11, 17, 12, 0, n, tzinfo=timezone.utc),
         "LogicalResourceId": resource, "ResourceStatus": status}
    return {**e, "ResourceStatusReason": reason} if reason else e


def test_deployer_saves_the_bundle_and_starts_the_workflow():
    class Recorder:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return lambda **kw: self.calls.append((name, kw))

    sfn, s3 = Recorder(), Recorder()
    d = StepFunctionsDeployer("arn:deploy", "arn:teardown", "artifacts",
                              {"stepfunctions": sfn, "s3": s3, "cloudformation": FakeCfn()})
    req = new()
    bundle = Bundle(request_id=req.id, stack_name=req.stack_name, template={"Resources": {}}, parameters={"A": "1"})
    assert d.start(req, bundle) is None
    assert s3.calls[0][1]["Key"] == bundle_key(req.id)
    name, kw = sfn.calls[0]
    body = json.loads(kw["input"])
    assert kw["name"] == req.id and kw["stateMachineArn"] == "arn:deploy"
    assert body["parameters"] == [{"ParameterKey": "A", "ParameterValue": "1"}]
    assert json.loads(body["template_body"]) == {"Resources": {}}

    req.stack_id = "arn:stack/ip-sbx/1"
    d.teardown(req)
    assert json.loads(sfn.calls[1][1]["input"])["stack"] == "arn:stack/ip-sbx/1"
    assert sfn.calls[1][1]["name"].startswith(req.id + "-")


def test_logs_come_oldest_first_and_only_newer_ones_after_next():
    req = new()
    cfn = FakeCfn({req.stack_name: {}}, events=[event(n) for n in (5, 4, 3, 2, 1)])  # newest first
    d = StepFunctionsDeployer("a", "b", "c", {"stepfunctions": None, "s3": None, "cloudformation": cfn})
    logs = d.logs(req, None)
    assert [e.id for e in logs.events] == ["e1", "e2", "e3", "e4", "e5"] and logs.next == "e5"
    cfn.events.insert(0, event(6, status="UPDATE_COMPLETE"))
    assert [e.id for e in d.logs(req, "e5").events] == ["e6"]
    assert d.logs(req, "e6").events == [] and d.logs(req, "e6").next == "e6"
    assert d.logs(new(), None).events == []  # stack not created yet


@pytest.fixture
def ops(table, monkeypatch):  # noqa: F811
    from infrapilot_api import sandbox_handler as h

    monkeypatch.setenv("REQUESTS_TABLE", "requests")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "artifacts")
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="artifacts")
    store = DynamoRequestStore("requests", table=table)
    h._clients.clear()
    h._clients.update({"s3": s3, "service": h.DeployService(store, None, None)})
    yield h, store, s3
    h._clients.clear()


class Ctx:
    function_name, memory_limit_in_mb, invoked_function_arn = "ops", 256, "arn:aws:lambda:us-east-1:1:function:ops"
    aws_request_id = "r"


def deploying(store):
    req = new()
    store.create(req)
    store.claim(req.user_id, req.id, status="deploying")
    return req


def test_deployed_uploads_the_sample_page_and_records_outputs(ops):
    h, store, s3 = ops
    req = deploying(store)
    site = req.stack_name + "-sitebucket-abc"
    s3.create_bucket(Bucket=site)
    bundle = Bundle(request_id=req.id, stack_name=req.stack_name, template={}, uploads={"index.html": "<h1>hi</h1>"},
                    upload_to="BucketName")
    s3.put_object(Bucket="artifacts", Key=bundle_key(req.id), Body=bundle.model_dump_json())
    h._clients["cloudformation"] = FakeCfn({"sid": {"StackStatus": "CREATE_COMPLETE", "Outputs": [
        {"OutputKey": "BucketName", "OutputValue": site}, {"OutputKey": "SiteUrl", "OutputValue": "https://d1"}]}})

    h.handler({"action": "started", "request_id": req.id, "stack_id": "sid"}, Ctx())
    assert store.get(req.id).stack_id == "sid"
    assert h.handler({"action": "status", "stack": "sid"}, Ctx()) == {"status": "CREATE_COMPLETE"}
    h.handler({"action": "deployed", "request_id": req.id, "stack": "sid"}, Ctx())
    got = store.get(req.id)
    assert got.status == "deployed" and got.outputs["SiteUrl"] == "https://d1"
    obj = s3.get_object(Bucket=site, Key="index.html")
    assert obj["ContentType"] == "text/html" and obj["Body"].read() == b"<h1>hi</h1>"


def test_failed_records_the_first_real_failure_and_frees_the_lock_when_the_stack_is_gone(ops):
    h, store, _ = ops
    req = deploying(store)
    h._clients["cloudformation"] = FakeCfn({"sid": {"StackStatus": "DELETE_COMPLETE"}}, events=[
        event(3, req.stack_name, "DELETE_COMPLETE"),
        event(2, "Function", "CREATE_FAILED", "Resource creation cancelled"),
        event(1, "Role", "CREATE_FAILED", "not authorized to perform iam:CreateRole"),
    ])
    out = h.handler({"action": "failed", "request_id": req.id, "stack": "sid", "error": "The deploy failed."}, Ctx())
    assert out["error"].startswith("Role: not authorized")
    assert store.get(req.id).status == "failed"
    other = new(req.user_id)
    store.create(other)
    assert store.claim(req.user_id, other.id, status="deploying").status == "deploying"  # lock was released


def test_teardown_empties_sandbox_buckets_only_and_records_torn_down(ops):
    h, store, s3 = ops
    req = deploying(store)
    store.transition(req.id, {"deploying"}, status="deployed")
    store.transition(req.id, {"deployed"}, status="tearing_down")
    bucket = req.stack_name + "-sitebucket-abc"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    for _ in range(2):
        s3.put_object(Bucket=bucket, Key="index.html", Body=b"x")
    h._clients["cloudformation"] = FakeCfn({"sid": {"StackStatus": "CREATE_COMPLETE"}}, resources=[
        {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": bucket, "ResourceStatus": "CREATE_COMPLETE"},
        {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "artifacts", "ResourceStatus": "CREATE_COMPLETE"},
    ])
    s3.put_object(Bucket="artifacts", Key="keep", Body=b"x")
    assert h.handler({"action": "empty", "stack": "sid"}, Ctx()) == {"emptied": [bucket]}
    assert "Versions" not in s3.list_object_versions(Bucket=bucket)
    assert s3.list_objects_v2(Bucket="artifacts")["KeyCount"] == 1
    assert h.handler({"action": "empty", "stack": "gone"}, Ctx()) == {"emptied": []}
    assert h.handler({"action": "status", "stack": "gone"}, Ctx()) == {"status": "GONE"}

    h.handler({"action": "torn_down", "request_id": req.id}, Ctx())
    assert store.get(req.id).status == "torn_down"
    assert store.get(f"ACTIVE#{req.user_id}") is None and \
        store.table.get_item(Key={"id": f"ACTIVE#{req.user_id}"}).get("Item") is None


def test_a_failed_teardown_keeps_the_sandbox_and_the_lock(ops):
    h, store, _ = ops
    req = deploying(store)
    store.transition(req.id, {"deploying"}, status="tearing_down")
    h.handler({"action": "teardown_failed", "request_id": req.id}, Ctx())
    got = store.get(req.id)
    assert got.status == "failed" and "retried" in got.error
    assert store.table.get_item(Key={"id": f"ACTIVE#{req.user_id}"})["Item"]["request_id"] == req.id
