"""The Lambda entry point with API Gateway HTTP API (payload v2) events, a fake answer service and
an in-memory store: routing, auth, status codes and JSON shapes."""

from __future__ import annotations

import json

import pytest

from infrapilot_api.service import ChatService
from infrapilot_api.sessions import InMemorySessionStore
from infrapilot_assistant.dialog import Assistant

from test_api import FakeService, ScriptedClassifier


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from infrapilot_api import lambda_handler as lh

    svc = FakeService()
    monkeypatch.setattr(lh, "chat", ChatService(InMemorySessionStore(), lambda: svc,
                                                lambda llm: Assistant(ScriptedClassifier())))
    return lh.lambda_handler


class Context:
    function_name, memory_limit_in_mb, invoked_function_arn = "chat", 512, "arn:aws:lambda:us-east-1:1:function:chat"
    aws_request_id = "req-1"


def event(method, path, user="alice", body=None):
    route = path if path in ("/health", "/sessions") else \
        "/sessions/{session_id}" + ("/messages" if path.endswith("/messages") else "")
    ctx = {"http": {"method": method, "path": path, "sourceIp": "1.2.3.4", "userAgent": "test"},
           "requestId": "r", "routeKey": f"{method} {route}", "stage": "$default"}
    if user:
        ctx["authorizer"] = {"jwt": {"claims": {"sub": user}, "scopes": None}}
    return {"version": "2.0", "routeKey": ctx["routeKey"], "rawPath": path, "rawQueryString": "",
            "headers": {"content-type": "application/json"}, "requestContext": ctx,
            "body": None if body is None else json.dumps(body), "isBase64Encoded": False}


def call(handler, *args, **kwargs):
    r = handler(event(*args, **kwargs), Context())
    return r["statusCode"], json.loads(r["body"])


def test_health(handler):
    assert call(handler, "GET", "/health", user=None) == (200, {"status": "ok"})


def test_create_ask_and_read_back(handler):
    status, created = call(handler, "POST", "/sessions")
    assert status == 201
    sid = created["id"]
    status, reply = call(handler, "POST", f"/sessions/{sid}/messages", body={"content": "How do I deploy to ECS?"})
    assert status == 200 and reply["type"] == "answer" and reply["message"]["citations"][0]["n"] == 1
    status, session = call(handler, "GET", f"/sessions/{sid}")
    assert status == 200 and [m["role"] for m in session["messages"]] == ["user", "assistant"]


def test_other_user_gets_404(handler):
    _, created = call(handler, "POST", "/sessions", user="alice")
    assert call(handler, "GET", f"/sessions/{created['id']}", user="bob")[0] == 404


def test_missing_user_is_401(handler):
    assert call(handler, "POST", "/sessions", user=None)[0] == 401


def test_invalid_body_is_422(handler):
    _, created = call(handler, "POST", "/sessions")
    for body in ({"content": ""}, {"content": "q", "filters": {"url": "x"}}, {}):
        assert call(handler, "POST", f"/sessions/{created['id']}/messages", body=body)[0] == 422


def test_request_routes(monkeypatch, handler):
    from infrapilot_api import lambda_handler as lh
    from infrapilot_api.artifacts import InMemoryPlanStore
    from infrapilot_api.deploy_requests import InMemoryRequestStore
    from infrapilot_api.deploys import DeployService, DryRunDeployer

    from test_deploys import lambda_plan

    assert call(handler, "GET", "/requests")[0] == 503  # REQUESTS_TABLE not set
    plans = InMemoryPlanStore()
    plans.save("alice", "s1", "m1", lambda_plan())
    monkeypatch.setattr(lh, "deploys", DeployService(InMemoryRequestStore(), plans, DryRunDeployer()))

    status, req = call(handler, "POST", "/requests", body={"session_id": "s1", "message_id": "m1"})
    assert status == 201 and req["status"] == "pending"
    assert call(handler, "GET", f"/requests/{req['id']}", user="bob")[0] == 404
    assert call(handler, "POST", f"/requests/{req['id']}/reject")[1]["status"] == "rejected"  # no body
    assert call(handler, "POST", "/requests", body={"session_id": "s1"})[0] == 422

    rid = call(handler, "POST", "/requests", body={"session_id": "s1", "message_id": "m1"})[1]["id"]
    assert call(handler, "POST", f"/requests/{rid}/approve")[1]["status"] == "deployed"
    status, logs = call(handler, "GET", f"/requests/{rid}/logs")
    assert status == 200 and len(logs["events"]) == 2
    assert call(handler, "POST", f"/requests/{rid}/teardown")[1]["status"] == "torn_down"
    status, history = call(handler, "GET", "/requests")
    assert status == 200 and [h["status"] for h in history] == ["torn_down", "rejected"]
