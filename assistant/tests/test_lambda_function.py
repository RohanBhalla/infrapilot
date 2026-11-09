"""Lambda generator: every trigger x runtime x CI combination must produce a template that cfn-lint
accepts and (with CI) a workflow that passes actionlint and the pin check."""

from __future__ import annotations

import itertools
import json
import os
import urllib.request

import pytest
import yaml

from infrapilot_assistant.dialog import _resolved
from infrapilot_assistant.generators import generate
from infrapilot_assistant.generators import lambda_function as lf
from infrapilot_assistant.intents import INTENTS

from test_github_actions import VERSIONS
from test_static_site import CfnLoader, needs_linters

CASES = list(itertools.product(lf.TRIGGERS, ("node", "python"), (True, False)))


def params(**kw):
    return _resolved(INTENTS["deploy_lambda"], kw, VERSIONS)


def plan_for(trigger, runtime=None, ci=True, **kw):
    return generate("deploy_lambda", params(trigger=trigger, ci_deploy=ci,
                                            **({"runtime": runtime} if runtime else {}), **kw), VERSIONS)


def template(plan):
    return yaml.load(plan.files[0].content, Loader=CfnLoader)


@needs_linters
@pytest.mark.parametrize("trigger,runtime,ci", CASES)
def test_every_combination_is_valid(trigger, runtime, ci):
    plan = plan_for(trigger, runtime, ci)
    status = {v.tool: v.status for v in plan.validations}
    assert status["cfn-lint"] == "pass" and "fail" not in status.values(), [v.findings for v in plan.validations]
    assert set(status) == ({"version-pins", "actionlint", "cfn-lint", "checkov"} if ci else {"cfn-lint", "checkov"})
    t = template(plan)
    fn = t["Resources"]["Function"]["Properties"]
    assert fn["Runtime"] == ("nodejs24.x" if runtime == "node" else "python3.14")
    assert fn["Architectures"] == ["arm64"] and fn["LoggingConfig"] == {"LogGroup": {"Ref": "LogGroup"}}
    assert ("GitHubDeployRole" in t["Resources"]) == ci
    expected = {"function_url": "FunctionUrl", "api_gateway": "Api", "schedule": "Schedule",
                "s3_event": "UploadBucket", "manual": None}[trigger]
    assert not expected or expected in t["Resources"]
    if ci:
        wf = yaml.safe_load(plan.files[1].content)
        names = [s["name"] for s in wf["jobs"]["deploy"]["steps"]]
        assert names[-1] == "Update the function code" and wf["jobs"]["deploy"]["permissions"]["id-token"] == "write"
        assert ("Set up Python" in names) == (runtime == "python")


@pytest.mark.parametrize("said,runtime", [
    (None, "nodejs24.x"), ("node", "nodejs24.x"), ("Node 22", "nodejs22.x"), ("nodejs24.x", "nodejs24.x"),
    ("typescript", "nodejs24.x"), ("python", "python3.14"), ("Python 3.13", "python3.13"),
    ("python3.13", "python3.13"), ("py", "python3.14"),
])
def test_runtime_is_read_from_what_the_user_said(said, runtime):
    v = {**VERSIONS, "lambda-runtime-python3.13": {"extra": {"status": "supported"}},
         "lambda-runtime-nodejs22.x": {"extra": {"status": "supported"}},
         "lambda-runtime-nodejs24.x": {"extra": {"status": "supported"}}}
    notes = []
    assert lf._runtime(said, v, notes)[0] == runtime and notes == []


def test_deprecated_or_unknown_runtimes_are_replaced_with_a_note():
    v = {**VERSIONS, "lambda-runtime-nodejs20.x": {"extra": {"status": "deprecated"}},
         "lambda-runtime-nodejs24.x": {"extra": {"status": "supported"}},
         "lambda-runtime-python3.12": {"extra": {"status": "supported", "deprecation": "2020-01-01"}}}
    for said, used in [("python3.9", "python3.14"), ("node 20", "nodejs24.x"), ("python3.12", "python3.14"),
                       ("ruby3.4", "nodejs24.x")]:
        notes = []
        assert lf._runtime(said, v, notes)[0] == used and len(notes) == 1, said


def test_function_role_only_writes_its_own_logs():
    t = template(plan_for("manual"))
    statements = t["Resources"]["FunctionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert statements == [{"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                           "Resource": {"Fn::GetAtt": ["LogGroup", "Arn"]}}]
    assert t["Resources"]["LogGroup"]["Properties"]["RetentionInDays"] == 30


def test_s3_trigger_reads_uploads_and_writes_results_elsewhere():
    t = template(plan_for("s3_event"))
    statements = t["Resources"]["FunctionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert [s["Action"] for s in statements[1:]] == ["s3:GetObject", "s3:PutObject"]
    assert "/uploads/*" in str(statements[1]["Resource"]) and "/processed/*" in str(statements[2]["Resource"])
    bucket = t["Resources"]["UploadBucket"]
    rule = bucket["Properties"]["NotificationConfiguration"]["LambdaConfigurations"][0]["Filter"]["S3Key"]["Rules"][0]
    assert rule == {"Name": "prefix", "Value": "uploads/"} and bucket["DependsOn"] == "UploadPermission"
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())


def test_deploy_role_trusts_one_repo_environment_and_touches_only_this_function():
    role = template(plan_for("api_gateway"))["Resources"]["GitHubDeployRole"]
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    assert trust["token.actions.githubusercontent.com:sub"] == {
        "Fn::Sub": "repo:${GitHubRepository}:environment:production"}
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert statements[0]["Resource"] == {"Fn::GetAtt": ["Function", "Arn"]} and "*" not in str(statements)


def test_function_url_is_public_and_says_so():
    plan = plan_for("function_url")
    t = template(plan)
    assert t["Resources"]["FunctionUrl"]["Properties"]["AuthType"] == "NONE"
    assert t["Resources"]["FunctionUrlInvokePermission"]["Properties"]["InvokedViaFunctionUrl"] is True
    assert any("public" in n for n in plan.notes)


@pytest.mark.parametrize("said,expression", [
    ("rate(1 hour)", "rate(1 hour)"), ("rate(1 hours)", "rate(1 hour)"), ("rate(15 minute)", "rate(15 minutes)"),
    ("cron(0 2 * * ? *)", "cron(0 2 * * ? *)"), ("every night", "rate(1 hour)"), ("cron(0 2 * *)", "rate(1 hour)"),
])
def test_schedule_expressions_are_fixed_or_replaced(said, expression):
    t = template(plan_for("schedule", schedule=said))
    assert t["Resources"]["Schedule"]["Properties"]["ScheduleExpression"] == expression


def test_schedule_default_only_applies_to_the_schedule_trigger():
    assert params(trigger="schedule")["schedule"] == "rate(1 hour)"
    assert "schedule" not in params(trigger="function_url")


def test_bad_inputs_fall_back_with_a_note():
    plan = plan_for("nope", source_dir="../../etc", handler="not a handler", memory_mb=99999, region="moon-1")
    notes = " ".join(plan.notes)
    for expected in ("don't know the trigger", "used `src`", "used `index.handler`", "between 128 and 10240",
                     "isn't an AWS region"):
        assert expected in notes, expected
    assert template(plan)["Resources"]["Function"]["Properties"]["MemorySize"] == 10240


def test_placeholder_matches_the_handler_function_and_warns_about_other_files():
    plan = plan_for("manual", "python", handler="app.main")
    code = template(plan)["Resources"]["Function"]["Properties"]["Code"]["ZipFile"]
    assert code.startswith("def main(event, context):")
    assert any("app.py" in n for n in plan.notes)


def test_setup_has_the_deploy_command():
    plan = plan_for("schedule")
    cmd = next(s for s in plan.setup if "cloudformation deploy" in s)
    assert "GitHubRepository=" in cmd and "CAPABILITY_IAM" in cmd and "lambda-function" in cmd
    assert "update-function-code" in next(s for s in plan_for("schedule", ci=False).setup if "zip" in s)


@pytest.mark.skipif(not os.environ.get("INFRAPILOT_VALIDATOR_URL"), reason="validator image not running")
@pytest.mark.parametrize("trigger,runtime,ci", CASES)
def test_every_combination_passes_every_linter_in_the_image(trigger, runtime, ci):
    plan = plan_for(trigger, runtime, ci)
    req = urllib.request.Request(os.environ["INFRAPILOT_VALIDATOR_URL"],
                                 json.dumps({"files": [f.model_dump() for f in plan.files]}).encode())
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    assert all(v["status"] == "pass" for v in body["validations"]), body
    assert "checkov" in {v["tool"] for v in body["validations"]}
