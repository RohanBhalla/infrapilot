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


# Just enough of the version registry for the GitHub Actions generator.
VERSIONS = {a: {"extra": {"major": m}} for a, m in [
    ("actions/checkout", "v7"), ("actions/setup-node", "v7"), ("aws-actions/configure-aws-credentials", "v6")]}


def make(svc=None, *queue):
    svc = svc or FakeService()
    clf = ScriptedClassifier(*queue)
    client = TestClient(create_app(service=svc, store=InMemorySessionStore(), assistant=Assistant(clf, VERSIONS)))
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
    assert r.status_code == 502 and store.get("local", sid).messages == []


def test_openapi_lists_endpoints(setup):
    client, *_ = setup
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/sessions", "/sessions/{session_id}", "/sessions/{session_id}/messages"} <= set(paths)


def test_root_redirects_to_docs(setup):
    client, *_ = setup
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/docs"


def test_ready_ci_task_returns_generated_workflow():
    client, svc, _ = make(None, Classification("cicd_github_actions", 0.95, "set up CI for my node app",
                                               {"language": "node", "deploy_target": "lambda"}))
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "set up CI for my node app"}).json()
    assert r["type"] == "ready" and r["plan"]["intent"] == "cicd_github_actions"
    files = r["message"]["files"]
    assert [f["path"] for f in files] == [".github/workflows/ci.yml"] and "aws lambda" in files[0]["content"]
    assert "Before the first run" in r["message"]["content"] and "Checks: version-pins passed" in r["message"]["content"]
    assert any("oidc-in-aws" in c["url"] for c in r["message"]["citations"])
    saved = client.get(f"/sessions/{sid}").json()["messages"][-1]
    assert saved["files"] == files and svc.calls == []


def test_ready_task_without_a_generator_says_so():
    client, _, _ = make(None, Classification("deploy_static_site", 0.95, "host my site", {"source_dir": "dist"}))
    sid = new_session(client)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "host my site from dist"}).json()
    assert r["type"] == "ready" and r["plan"] is None and r["message"]["files"] == []
    assert "isn't available yet" in r["message"]["content"]


class FakePlans:
    def __init__(self, fail=False):
        self.saved, self.fail = [], fail

    def save(self, user, session_id, message_id, plan):
        if self.fail:
            raise RuntimeError("s3 down")
        self.saved.append((user, session_id, message_id, plan.intent))


@pytest.mark.parametrize("fail", [False, True])
def test_ready_plans_are_saved_and_a_failed_save_doesnt_break_the_reply(fail):
    from infrapilot_api.schemas import MessageIn
    from infrapilot_api.service import ChatService

    plans = FakePlans(fail)
    clf = ScriptedClassifier(Classification("cicd_github_actions", 0.95, "CI for node", {"language": "node"}))
    chat = ChatService(InMemorySessionStore(), FakeService, lambda llm: Assistant(clf, VERSIONS), plans=plans)
    sid = chat.create_session("u1").id
    out = chat.post_message("u1", sid, MessageIn(content="CI for node"))
    assert out.type == "ready" and out.message.files
    assert plans.saved == ([] if fail else [("u1", sid, out.message.id, "cicd_github_actions")])


def test_s3_plan_store_writes_json_under_the_users_prefix():
    from infrapilot_api.artifacts import S3PlanStore
    from infrapilot_assistant.plan import Plan

    class FakeS3:
        def put_object(self, **kw):
            self.kw = kw

    s3 = FakeS3()
    key = S3PlanStore("bucket", client=s3).save("u1", "s1", "m1", Plan(intent="x", params={}, files=[], summary="s"))
    assert key == "plans/u1/s1/m1.json" == s3.kw["Key"] and s3.kw["Bucket"] == "bucket"
    assert Plan.model_validate_json(s3.kw["Body"]).intent == "x"
