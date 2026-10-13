"""Offline API tests: fake answer service and scripted classifier, no network or cost."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from infrapilot_api.app import create_app
from infrapilot_assistant.classifier import Classification
from infrapilot_assistant.dialog import Assistant
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


class ScriptedClassifier:
    """Returns queued classifications; defaults to a plain question."""

    def __init__(self, *queue):
        self.queue, self.calls = list(queue), 0

    def classify(self, message, state, history=()):
        self.calls += 1
        if self.queue:
            c = self.queue.pop(0)
            if isinstance(c, Exception):
                raise c
            return c
        return Classification("qa", 0.95, message, cost_usd=0.001)


def make(svc=None, *queue):
    svc = svc or FakeService()
    clf = ScriptedClassifier(*queue)
    client = TestClient(create_app(service=svc, store=InMemorySessionStore(), assistant=Assistant(clf)))
    return client, svc, clf


@pytest.fixture
def setup():
    return make()


def new_session(client) -> str:
    r = client.post("/sessions")
    assert r.status_code == 201
    return r.json()["id"]


def test_health(setup):
    client, *_ = setup
    assert client.get("/health").json() == {"status": "ok"}


def test_question_is_answered_with_citations(setup):
    client, svc, clf = setup
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    body = r.json()
    assert r.status_code == 200 and body["type"] == "answer" and body["intent"] == "qa"
    assert body["message"]["citations"][0]["n"] == 1 and clf.calls == 1
    assert body["cost_usd"] == pytest.approx(0.004)  # classifier 0.001 + answer 0.003


def test_follow_up_uses_the_standalone_question():
    client, svc, _ = make(None, Classification("qa", 0.9, "How do I deploy to ECS?"),
                          Classification("qa", 0.9, "How do I deploy to ECS on Fargate?"))
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    r = client.post(f"/sessions/{sid}/messages", json={"content": "and on Fargate?"})
    assert r.json()["standalone_question"] == "How do I deploy to ECS on Fargate?"
    assert svc.calls[-1][0] == "How do I deploy to ECS on Fargate?"


def test_task_flow_clarifies_then_ready_and_state_is_saved():
    client, svc, _ = make(None,
                          Classification("deploy_static_site", 0.95, "host my react app", {"framework": "react"}),
                          Classification("deploy_static_site", 0.95, "it's in dist", {"source_dir": "dist"}))
    sid = new_session(client)
    r1 = client.post(f"/sessions/{sid}/messages", json={"content": "host my react app on s3"}).json()
    assert r1["type"] == "clarification" and r1["questions"][0]["slot"] == "source_dir"
    assert client.get(f"/sessions/{sid}").json()["dialog"]["intent"] == "deploy_static_site"
    r2 = client.post(f"/sessions/{sid}/messages", json={"content": "dist"}).json()
    assert r2["type"] == "ready" and r2["params"]["source_dir"] == "dist" and r2["params"]["spa_routing"] is True
    assert svc.calls == []  # no RAG answer needed for task turns


def test_classifier_failure_still_answers():
    client, svc, _ = make(None, RuntimeError("openai down"))
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "How do I deploy to ECS?"})
    assert r.status_code == 200 and r.json()["type"] == "answer" and svc.calls[-1][0] == "How do I deploy to ECS?"


def test_explain_failure_sends_error_text_to_search():
    client, svc, _ = make(None, Classification("explain_failure", 0.9, "Why does my ECS task fail?",
                                               {"error_text": "CannotPullContainerError: access denied"}))
    sid = new_session(client)
    client.post(f"/sessions/{sid}/messages", json={"content": "task failed: CannotPullContainerError: access denied"})
    assert "CannotPullContainerError" in svc.calls[-1][0]


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
                                   assistant=Assistant(ScriptedClassifier())))
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "q"})
    assert r.status_code == 502 and store.get(sid).messages == []


def test_openapi_lists_endpoints(setup):
    client, *_ = setup
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/sessions", "/sessions/{session_id}", "/sessions/{session_id}/messages"} <= set(paths)


def test_root_redirects_to_docs(setup):
    client, *_ = setup
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/docs"
