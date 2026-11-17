"""Deploy requests: the flow through the local app (dry-run deployer), the rules in DeployService,
and DynamoRequestStore against moto. No AWS calls."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from infrapilot_api.artifacts import InMemoryPlanStore
from infrapilot_api.deploy_requests import (Conflict, DeployRequest, DynamoRequestStore, InMemoryRequestStore,
                                            new_request_id)
from infrapilot_api.deploys import DeployService, DryRunDeployer, NotDeployableError, Outcome
from infrapilot_assistant.classifier import Classification

from test_api import VERSIONS, make, new_session

LAMBDA = Classification("deploy_lambda", 0.95, "deploy my function", {"trigger": "schedule"})
CI = Classification("cicd_github_actions", 0.95, "set up CI for my node app", {"language": "node"})


def ready_reply(*classifications):
    client, _, _ = make(None, *classifications)
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "run my function every hour on lambda"}).json()
    assert r["type"] == "ready"
    return client, sid, r["message"]["id"]


def test_request_approve_logs_teardown():
    client, sid, mid = ready_reply(LAMBDA)
    r = client.post("/requests", json={"session_id": sid, "message_id": mid})
    assert r.status_code == 201
    req = r.json()
    assert req["status"] == "pending" and req["intent"] == "deploy_lambda"
    assert req["stack_name"] == "ip-sbx-" + req["id"] and len(req["id"]) == 10
    assert client.get(f"/requests/{req['id']}/logs").json() == {"events": [], "next": None}

    done = client.post(f"/requests/{req['id']}/approve").json()
    assert done["status"] == "deployed" and done["teardown_at"] and done["outputs"]
    logs = client.get(f"/requests/{req['id']}/logs").json()
    assert [e["status"] for e in logs["events"]] == ["CREATE_IN_PROGRESS", "CREATE_COMPLETE"]
    assert client.get(f"/requests/{req['id']}/logs", params={"next": logs["next"]}).json()["events"] == []

    assert client.post(f"/requests/{req['id']}/approve").status_code == 409  # not pending any more
    assert client.post(f"/requests/{req['id']}/teardown").json()["status"] == "torn_down"
    assert client.post(f"/requests/{req['id']}/teardown").status_code == 409
    assert [h["id"] for h in client.get("/requests").json()] == [req["id"]]


def test_one_active_sandbox_per_user():
    client, sid, mid = ready_reply(LAMBDA)
    first, second = (client.post("/requests", json={"session_id": sid, "message_id": mid}).json()["id"]
                     for _ in range(2))
    assert client.post(f"/requests/{first}/approve").status_code == 200
    r = client.post(f"/requests/{second}/approve")
    assert r.status_code == 409 and first in r.json()["detail"]
    assert client.get(f"/requests/{second}").json()["status"] == "pending"  # can be approved later
    client.post(f"/requests/{first}/teardown")
    assert client.post(f"/requests/{second}/approve").json()["status"] == "deployed"


def test_reject_keeps_feedback_and_blocks_approval():
    client, sid, mid = ready_reply(LAMBDA)
    rid = client.post("/requests", json={"session_id": sid, "message_id": mid}).json()["id"]
    r = client.post(f"/requests/{rid}/reject", json={"feedback": "use python"}).json()
    assert r["status"] == "rejected" and r["feedback"] == "use python"
    assert client.post(f"/requests/{rid}/approve").status_code == 409


def test_workflow_only_plan_is_refused_with_the_reason():
    client, sid, mid = ready_reply(CI)
    r = client.post("/requests", json={"session_id": sid, "message_id": mid})
    assert r.status_code == 422 and "no AWS template" in r.json()["detail"]
    assert client.get("/requests").json() == []


def test_unknown_plan_and_bad_ids():
    client, sid, _ = ready_reply(LAMBDA)
    assert client.post("/requests", json={"session_id": sid, "message_id": "nope"}).status_code == 404
    assert client.post("/requests", json={"session_id": sid}).status_code == 422
    assert client.get("/requests/NOT-AN-ID").status_code == 404
    assert client.get("/requests/abcdefghij").status_code == 404


# DeployService rules, without the web layer ------------------------------------------------------

def lambda_plan():
    from infrapilot_assistant.generators import generate

    return generate("deploy_lambda", {"trigger": "function_url", "language": "python"}, VERSIONS)


class FlakyDeployer(DryRunDeployer):
    def __init__(self, fail_start=False, fail_teardown=False, run_async=False):
        self.fail_start, self.fail_teardown, self.run_async = fail_start, fail_teardown, run_async

    def start(self, req, bundle):
        if self.fail_start:
            raise RuntimeError("states down")
        assert bundle.stack_name == req.stack_name and bundle.template["Resources"]
        return None if self.run_async else super().start(req, bundle)

    def teardown(self, req):
        if self.fail_teardown:
            raise RuntimeError("states down")
        return None if self.run_async else super().teardown(req)


def service(deployer):
    plans = InMemoryPlanStore()
    plans.save("alice", "s1", "m1", lambda_plan())
    return DeployService(InMemoryRequestStore(), plans, deployer)


def test_other_users_cant_see_or_change_a_request():
    svc = service(DryRunDeployer())
    rid = svc.create("alice", "s1", "m1").id
    for call in (svc.get, svc.approve, svc.teardown, svc.logs):
        with pytest.raises(Exception) as exc:
            call("mallory", rid)
        assert exc.value.status == 404
    with pytest.raises(Exception) as exc:
        svc.create("mallory", "s1", "m1")  # plans are looked up under the caller's own prefix
    assert exc.value.status == 404


def test_a_deploy_that_doesnt_start_is_failed_and_frees_the_lock():
    svc = service(FlakyDeployer(fail_start=True))
    req = svc.approve("alice", svc.create("alice", "s1", "m1").id)
    assert req.status == "failed" and "didn't start" in req.error
    svc.deployer = DryRunDeployer()
    assert svc.approve("alice", svc.create("alice", "s1", "m1").id).status == "deployed"


def test_async_deployer_reports_back_through_record():
    svc = service(FlakyDeployer(run_async=True))
    rid = svc.create("alice", "s1", "m1").id
    assert svc.approve("alice", rid).status == "deploying"
    svc.record(rid, Outcome(status="deployed", outputs={"FunctionUrl": "https://x"}))
    assert svc.teardown("alice", rid).status == "tearing_down"
    with pytest.raises(Exception):
        svc.approve("alice", svc.create("alice", "s1", "m1").id)  # still active until it's gone
    assert svc.record(rid, Outcome(status="torn_down")).outputs == {"FunctionUrl": "https://x"}
    assert svc.approve("alice", svc.create("alice", "s1", "m1").id).status == "deploying"


def test_teardown_that_doesnt_start_can_be_retried():
    svc = service(FlakyDeployer(fail_teardown=True))
    rid = svc.create("alice", "s1", "m1").id
    svc.approve("alice", rid)
    with pytest.raises(RuntimeError):
        svc.teardown("alice", rid)
    assert svc.get("alice", rid).status == "deployed"
    svc.deployer = DryRunDeployer()
    assert svc.teardown("alice", rid).status == "torn_down"


def test_invalid_plan_is_refused():
    svc = service(DryRunDeployer())
    plan = lambda_plan()
    plan.validations[0].status = "fail"
    svc.plans.save("alice", "s1", "bad", plan)
    with pytest.raises(NotDeployableError):
        svc.create("alice", "s1", "bad")


# DynamoRequestStore ---------------------------------------------------------------------------

@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="requests", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("id", "user_id", "created_at")],
            GlobalSecondaryIndexes=[{"IndexName": "by_user", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"},
                                                   {"AttributeName": "created_at", "KeyType": "RANGE"}]}])


def new(user="alice") -> DeployRequest:
    rid = new_request_id()
    return DeployRequest(id=rid, user_id=user, session_id="s", message_id="m", plan_key="k", intent="deploy_lambda",
                         summary="x", stack_name="ip-sbx-" + rid)


def test_dynamo_store_round_trip_history_and_transitions(table):
    store = DynamoRequestStore("requests", table=table)
    a, b, other = new(), new(), new("bob")
    for r in (a, b, other):
        store.create(r)
    assert store.get(a.id) == a
    assert [r.id for r in store.list("alice")] == [b.id, a.id]  # newest first

    got = store.claim("alice", a.id, status="deploying", teardown_at=a.created_at)
    assert got.status == "deploying" and got.teardown_at == a.created_at
    with pytest.raises(Conflict) as exc:
        store.claim("alice", b.id, status="deploying")
    assert exc.value.active_id == a.id and store.get(b.id).status == "pending"
    with pytest.raises(Conflict) as exc:
        store.claim("alice", a.id, status="deploying")  # not pending: no lock id in the error
    assert exc.value.active_id is None
    store.claim("bob", other.id, status="deploying")  # locks are per user

    done = store.transition(a.id, {"deploying"}, status="deployed", outputs={"Url": "https://x"}, error=None)
    assert done.outputs == {"Url": "https://x"} and done.status == "deployed"
    with pytest.raises(Conflict):
        store.transition(a.id, {"deploying"}, status="failed")
    store.release("alice", b.id)  # not the holder: no effect
    with pytest.raises(Conflict):
        store.claim("alice", b.id, status="deploying")
    store.release("alice", a.id)
    assert store.claim("alice", b.id, status="deploying").status == "deploying"
    assert store.get("ACTIVE#alice") is None  # the lock item isn't a request
