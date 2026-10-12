"""Offline API tests: fake answer service and rewriter, no network or cost."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from infrapilot_api.app import create_app
from infrapilot_api.sessions import InMemorySessionStore
from infrapilot_rag.models import Answer, Citation


class FakeService:
    def __init__(self, fail: Exception | None = None):
        self.calls, self.fail = [], fail
        self.llm = None

    def ask(self, question, filters=None, k=None):
        if self.fail:
            raise self.fail
        self.calls.append((question, filters))
        return Answer(question=question, text=f"Answer to: {question} [1]", refused=False,
                      citations=[Citation(n=1, title="Doc", url="https://example.com/doc", chunk_id="c1")],
                      model="fake", cost_usd=0.003)


class FakeRewriter:
    model_id = "fake"

    def __init__(self, reply="How do I deploy to ECS on Fargate?", fail=False):
        self.reply, self.fail, self.calls = reply, fail, 0

    def complete(self, system, user):
        self.calls += 1
        if self.fail:
            raise RuntimeError("timeout")
        return self.reply, {"input_tokens": 50, "output_tokens": 10}


@pytest.fixture
def setup():
    svc, rw = FakeService(), FakeRewriter()
    return TestClient(create_app(service=svc, store=InMemorySessionStore(), rewriter=rw)), svc, rw


def new_session(client) -> str:
    r = client.post("/sessions")
    assert r.status_code == 201
    return r.json()["id"]


def test_health(setup):
    client, *_ = setup
    assert client.get("/health").json() == {"status": "ok"}


def test_first_message_answers_without_rewrite(setup):
    client, svc, rw = setup
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"]["role"] == "assistant" and body["message"]["citations"][0]["n"] == 1
    assert body["standalone_question"] == "How do I deploy to ECS?" and rw.calls == 0
    assert body["cost_usd"] == pytest.approx(0.003)


def test_follow_up_is_rewritten_using_history(setup):
    client, svc, rw = setup
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    r = client.post(f"/sessions/{sid}/messages", json={"content": "and on Fargate?"})
    assert rw.calls == 1
    assert r.json()["standalone_question"] == "How do I deploy to ECS on Fargate?"
    assert svc.calls[-1][0] == "How do I deploy to ECS on Fargate?"


def test_rewrite_failure_falls_back_to_original_message():
    svc = FakeService()
    client = TestClient(create_app(service=svc, store=InMemorySessionStore(), rewriter=FakeRewriter(fail=True)))
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    r = client.post(f"/sessions/{sid}/messages", json={"content": "and on Fargate?"})
    assert r.status_code == 200 and r.json()["standalone_question"] == "and on Fargate?"


def test_history_is_kept(setup):
    client, *_ = setup
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "first question"})
    msgs = client.get(f"/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["content"] == "first question"


def test_unknown_session_is_404(setup):
    client, *_ = setup
    assert client.get("/sessions/nope").status_code == 404
    assert client.post("/sessions/nope/messages", json={"content": "hi"}).status_code == 404


def test_validation_errors(setup):
    client, *_ = setup
    sid = new_session(client)
    assert client.post(f"/sessions/{sid}/messages", json={"content": ""}).status_code == 422
    assert client.post(f"/sessions/{sid}/messages", json={"content": "x" * 2001}).status_code == 422
    assert client.post(f"/sessions/{sid}/messages", json={"content": "q", "filters": {"url": "x"}}).status_code == 422


def test_filters_are_passed_through(setup):
    client, svc, _ = setup
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "q", "filters": {"category": "IaC – CDK"}})
    assert svc.calls[-1][1] == {"category": "IaC – CDK"}


def test_upstream_failure_is_502_and_not_stored():
    store = InMemorySessionStore()
    client = TestClient(create_app(service=FakeService(fail=RuntimeError("openai down")), store=store,
                                   rewriter=FakeRewriter()))
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "q"})
    assert r.status_code == 502 and store.get(sid).messages == []


def test_openapi_lists_endpoints(setup):
    client, *_ = setup
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/sessions", "/sessions/{session_id}", "/sessions/{session_id}/messages"} <= set(paths)
