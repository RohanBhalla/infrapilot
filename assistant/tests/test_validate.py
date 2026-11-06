"""The linters' routing: which files each one reads, the validator Lambda call and its fallback.

Set INFRAPILOT_VALIDATOR_URL to a running validator image to check every generated workflow
against all the linters:

    docker build --platform linux/arm64 -t infrapilot-validator -f validator/Dockerfile .
    docker run --rm -p 9000:8080 infrapilot-validator
    INFRAPILOT_VALIDATOR_URL=http://localhost:9000/2015-03-31/functions/function/invocations pytest -q
"""

from __future__ import annotations

import io
import json
import os
import urllib.request

import pytest

from infrapilot_assistant.generators import generate, validate
from infrapilot_assistant.plan import PlanFile

from test_github_actions import CASES, VERSIONS, params

WORKFLOW = PlanFile(path=".github/workflows/ci.yml", content="on: push\njobs: {}\n")
DOCKERFILE = PlanFile(path="Dockerfile", content="FROM python:3.14-slim\n", language="dockerfile")
TEMPLATE = PlanFile(path="template.yaml", content="AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n")
README = PlanFile(path="README.md", content="# hi\n", language="markdown")


def test_each_linter_reads_only_its_files():
    assert validate._applicable([WORKFLOW]) == ["actionlint", "checkov"]
    assert validate._applicable([DOCKERFILE]) == ["hadolint", "checkov"]
    assert validate._applicable([TEMPLATE]) == ["cfn-lint", "checkov"]
    assert validate._applicable([README]) == []


def test_missing_linters_are_skipped_not_failed(monkeypatch):
    monkeypatch.setattr(validate, "_find", lambda tool: None)
    assert {(v.tool, v.status) for v in validate.run_local([WORKFLOW, DOCKERFILE])} == {
        ("actionlint", "skipped"), ("hadolint", "skipped"), ("checkov", "skipped")}


def test_paths_cannot_leave_the_temp_folder():
    with pytest.raises(ValueError, match="bad file path"):
        validate.run_local([PlanFile(path="../../etc/x", content="")])


class FakeLambda:
    def __init__(self, body, error=None):
        self.body, self.error, self.calls = body, error, []

    def invoke(self, **kw):
        self.calls.append(kw)
        out = {"Payload": io.BytesIO(json.dumps(self.body).encode())}
        return out | ({"FunctionError": "Unhandled"} if self.error else {})


def test_remote_results_come_back_as_validations():
    fake = FakeLambda({"validations": [{"tool": "actionlint", "status": "pass", "findings": []}]})
    out = validate.run_remote([WORKFLOW], "validator", client=fake)
    assert [(v.tool, v.status) for v in out] == [("actionlint", "pass")]
    assert json.loads(fake.calls[0]["Payload"])["files"][0]["path"] == WORKFLOW.path


def test_remote_errors_skip_the_checks_instead_of_failing_the_chat():
    out = validate.run_remote([WORKFLOW], "validator", client=FakeLambda({"errorMessage": "boom"}, error=True))
    assert [(v.tool, v.status) for v in out] == [("actionlint", "skipped"), ("checkov", "skipped")]


def test_run_uses_the_lambda_when_configured(monkeypatch):
    seen = []
    monkeypatch.setenv("INFRAPILOT_VALIDATOR_FUNCTION", "validator")
    monkeypatch.setattr(validate, "run_remote", lambda files, fn: seen.append(fn) or [])
    validate.run([WORKFLOW])
    assert seen == ["validator"]


def test_handler_returns_plain_json():
    out = validate.lambda_handler({"files": [README.model_dump()]}, None)
    assert out == {"validations": []}


@pytest.mark.skipif(not os.environ.get("INFRAPILOT_VALIDATOR_URL"), reason="validator image not running")
@pytest.mark.parametrize("language,pm,deploy", CASES)
def test_every_workflow_passes_every_linter_in_the_image(language, pm, deploy):
    plan = generate("cicd_github_actions", params(language=language, package_manager=pm or None,
                                                  deploy_target=deploy), VERSIONS)
    req = urllib.request.Request(os.environ["INFRAPILOT_VALIDATOR_URL"],
                                 json.dumps({"files": [f.model_dump() for f in plan.files]}).encode())
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    assert {v["tool"]: v["status"] for v in body["validations"]} == {"actionlint": "pass", "checkov": "pass"}, body
