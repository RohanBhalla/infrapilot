"""Offline tests for the dialog rules and the classifier's parsing (fake LLM)."""

from __future__ import annotations

import json

from infrapilot_assistant.classifier import Classification, Classifier
from infrapilot_assistant.dialog import parse_value, step
from infrapilot_assistant.intents import INTENTS
from infrapilot_assistant.state import DialogState

VERSIONS = {"nodejs": {"lts": "24.21.0"}, "amazon-eks": {"latest": "1.37-eks-4"}}


def C(intent, conf=0.95, params=None, q="q"):
    return Classification(intent=intent, confidence=conf, standalone_question=q, params=params or {})


def test_question_is_answered_and_leaves_task_alone():
    st = DialogState(intent="deploy_lambda", params={"trigger": "schedule"}, status="collecting")
    t = step("how does the ECS circuit breaker work?", st, C("qa"))
    assert t.type == "answer" and st.intent == "deploy_lambda" and st.params == {"trigger": "schedule"}


def test_missing_required_detail_is_asked_with_defaults_once():
    st = DialogState()
    t = step("host my react app on s3", st, C("deploy_static_site", params={"framework": "react"}), VERSIONS)
    assert t.type == "clarification" and [q.slot for q in t.questions] == ["source_dir"]
    assert "spa routing: yes" in t.message and "region: us-east-1" in t.message
    t2 = step("hmm", st, C("deploy_static_site", conf=0.9), VERSIONS)
    assert t2.type == "clarification" and "Unless you say otherwise" not in t2.message


def test_answering_the_question_makes_it_ready_with_defaults():
    st = DialogState()
    step("host my react app", st, C("deploy_static_site", params={"framework": "react"}), VERSIONS)
    t = step("it's in dist", st, C("deploy_static_site", conf=0.6, params={"source_dir": "dist"}), VERSIONS)
    assert t.type == "ready"  # low confidence ignored: we're continuing the same task
    assert t.params["source_dir"] == "dist" and t.params["spa_routing"] is True and t.params["region"] == "us-east-1"


def test_defaults_come_from_version_registry():
    st = DialogState()
    t = step("deploy a lambda behind a url", st, C("deploy_lambda", params={"trigger": "function_url"}), VERSIONS)
    assert t.type == "ready" and t.params["runtime"] == "nodejs24.x"
    t = step("create eks, I accept the cost", DialogState(), C("eks_cluster", params={"cost_ack": "yes"}), VERSIONS)
    assert t.params["k8s_version"] == "1.37"


def test_low_confidence_new_task_asks_for_confirmation_then_proceeds():
    st = DialogState()
    t = step("lambda stuff", st, C("deploy_lambda", conf=0.5))
    assert t.type == "confirmation" and st.awaiting_confirmation
    t = step("yes please", st, C("deploy_lambda", conf=0.6))
    assert t.type == "clarification" and t.questions[0].slot == "trigger"


def test_declining_confirmation_drops_the_task():
    st = DialogState()
    step("lambda stuff", st, C("deploy_lambda", conf=0.5))
    t = step("no, just explain how lambda works", st, C("qa"))
    assert t.type == "answer" and st.intent is None


def test_switching_task_starts_fresh():
    st = DialogState(intent="deploy_lambda", params={"trigger": "schedule"}, status="collecting")
    t = step("actually write a dockerfile", st, C("containerize", params={"language": "node"}))
    assert st.intent == "containerize" and "trigger" not in st.params and t.questions[0].slot == "app_port"


def test_invalid_values_are_ignored_and_asked_again():
    st = DialogState()
    t = step("container on port banana", st, C("containerize", params={"language": "node", "app_port": "banana"}))
    assert [q.slot for q in t.questions] == ["app_port"]
    assert parse_value(INTENTS["containerize"].slot("app_port"), "70000") is None
    assert parse_value(INTENTS["containerize"].slot("language"), "TypeScript") == "node"
    assert parse_value(INTENTS["containerize"].slot("push_to_ecr"), "no") is False


def test_eks_needs_cost_ack_and_can_be_declined():
    st = DialogState()
    t = step("create an eks cluster", st, C("eks_cluster"))
    assert t.questions[0].slot == "cost_ack" and "$0.10/hour" in t.questions[0].question
    t = step("no thanks", st, C("eks_cluster", params={"cost_ack": "false"}))
    assert t.type == "out_of_scope" and st.status == "cancelled"


def test_explain_failure_asks_for_the_log_when_missing():
    t = step("my build failed", DialogState(), C("explain_failure"))
    assert t.type == "clarification" and t.questions[0].slot == "error_text"
    t = step("x", DialogState(), C("explain_failure", params={"error_text": "Error: AccessDenied"}))
    assert t.type == "answer" and t.error_text == "Error: AccessDenied"


def test_out_of_scope():
    assert step("tell me a joke", DialogState(), C("other")).type == "out_of_scope"


def test_changing_a_detail_after_ready():
    st = DialogState()
    step("lambda on a schedule", st, C("deploy_lambda", params={"trigger": "schedule"}), VERSIONS)
    t = step("use us-west-2 instead", st, C("deploy_lambda", conf=0.8, params={"region": "us-west-2"}), VERSIONS)
    assert t.type == "ready" and t.params["region"] == "us-west-2" and t.params["trigger"] == "schedule"


class FakeLLM:
    model_id = "fake"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, system, user, json_schema=None):
        self.calls.append((user, json_schema))
        return self.reply, {"input_tokens": 10, "output_tokens": 5}


def test_classifier_parses_structured_output_and_sends_state():
    reply = json.dumps({"intent": "deploy_static_site", "confidence": 0.9, "standalone_question": "Host it",
                        "params": [{"slot": "source_dir", "value": "dist"}, {"slot": "region", "value": ""}]})
    llm = FakeLLM(reply)
    st = DialogState(intent="deploy_static_site", params={"framework": "react"}, asked=["source_dir"])
    c = Classifier(llm).classify("dist", st)
    assert c.intent == "deploy_static_site" and c.params == {"source_dir": "dist"}
    assert llm.calls[0][1]["name"] == "classification" and "assistant last asked about: source_dir" in llm.calls[0][0]


def test_classifier_falls_back_to_qa_on_bad_output():
    c = Classifier(FakeLLM("not json")).classify("how do I x?", DialogState())
    assert c.intent == "qa" and c.standalone_question == "how do I x?"


def test_model_cannot_consent_to_cost_on_its_own():
    st = DialogState()
    t = step("Spin up a Kubernetes cluster on AWS", st, C("eks_cluster", params={"cost_ack": "true"}))
    assert t.type == "clarification" and t.questions[0].slot == "cost_ack"
    t = step("yes, fine", st, C("eks_cluster", params={"cost_ack": "true"}))  # answering our question
    assert t.type == "ready"
    t = step("EKS please, I know it costs money", DialogState(), C("eks_cluster", params={"cost_ack": "yes"}))
    assert t.type == "ready"


def test_placeholder_values_are_rejected():
    st = DialogState()
    t = step("Deploy my portfolio website", st,
             C("deploy_static_site", params={"source_dir": "unknown", "region": "n/a"}))
    assert t.type == "clarification" and "source_dir" not in st.params and "region" not in st.params


def test_model_cannot_refuse_cost_on_its_own():
    t = step("I need a k8s cluster for testing", DialogState(), C("eks_cluster", params={"cost_ack": "false"}))
    assert t.type == "clarification" and t.questions[0].slot == "cost_ack"
