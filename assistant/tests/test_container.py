"""Dockerfile generator: every language x ECR x ECS combination must produce files that pass cfn-lint,
actionlint and the pin check here, and hadolint and checkov in the validator image."""

from __future__ import annotations

import itertools
import json
import os
import re
import urllib.request

import pytest
import yaml

from infrapilot_assistant.dialog import _resolved
from infrapilot_assistant.generators import container as ct
from infrapilot_assistant.generators import generate
from infrapilot_assistant.intents import INTENTS

from test_github_actions import VERSIONS
from test_static_site import CfnLoader, needs_linters

CASES = list(itertools.product(ct.LANGUAGES, (True, False), (True, False)))


def plan_for(language, push=True, ecs=False, **kw):
    params = _resolved(INTENTS["containerize"], {"language": language, "app_port": 3000, "push_to_ecr": push,
                                                 "deploy_to_ecs": ecs, **kw}, VERSIONS)
    return generate("containerize", params, VERSIONS)


def file(plan, path):
    return next(f.content for f in plan.files if f.path == path)


def template(plan):
    return yaml.load(file(plan, ct.TEMPLATE_PATH), Loader=CfnLoader)


@needs_linters
@pytest.mark.parametrize("language,push,ecs", CASES)
def test_every_combination_is_valid(language, push, ecs):
    plan = plan_for(language, push, ecs)
    status = {v.tool: v.status for v in plan.validations}
    assert "fail" not in status.values(), [v.findings for v in plan.validations]
    pushed = push or ecs
    assert [f.path for f in plan.files] == ["Dockerfile", ".dockerignore"] + (
        [ct.TEMPLATE_PATH, ct.WORKFLOW_PATH] if pushed else [])
    assert set(status) == ({"version-pins", "actionlint", "hadolint", "cfn-lint", "checkov"} if pushed
                           else {"hadolint", "checkov"})
    if pushed:
        assert status["cfn-lint"] == "pass" and status["actionlint"] == "pass"
        t = template(plan)
        assert ("Service" in t["Resources"]) == ecs and "Repository" in t["Resources"]
        wf = yaml.safe_load(file(plan, ct.WORKFLOW_PATH))
        names = [s["name"] for s in wf["jobs"]["deploy"]["steps"]]
        assert ("Roll out the new image" in names) == ecs
        build = next(s for s in wf["jobs"]["deploy"]["steps"] if s["name"] == "Build and push the image")
        assert build["with"]["platforms"] == "linux/arm64"


@pytest.mark.parametrize("language", ct.LANGUAGES)
def test_dockerfile_is_two_stage_non_root_and_checked(language):
    df = file(plan_for(language, push=False), "Dockerfile")
    assert len(re.findall(r"^FROM ", df, re.M)) == 2
    user = re.findall(r"^USER (\S+)", df, re.M)
    assert user and re.fullmatch(r"\d+:\d+", user[-1]) and not user[-1].startswith("0:")
    assert "EXPOSE 3000" in df and ("HEALTHCHECK" in df) == (language != "go")
    assert ":latest" not in df and all(not re.search(r"^[A-Z]+ .*\s#", line) for line in df.splitlines())


def test_base_images_come_from_the_registry():
    assert f"{ct.MIRROR}/node:24-slim" in file(plan_for("node", push=False), "Dockerfile")
    assert f"{ct.MIRROR}/python:3.14-slim" in file(plan_for("python", push=False), "Dockerfile")
    java = file(plan_for("java", push=False), "Dockerfile")
    assert "maven:3.9-eclipse-temurin-25" in java and "eclipse-temurin:25-jre" in java


@pytest.mark.parametrize("start,cmd", [
    ("npm start", '["npm", "start"]'), ("node dist/server.js", '["node", "dist/server.js"]'),
    ("npm run migrate && npm start", '["sh", "-c", "npm run migrate && npm start"]'),
])
def test_start_command_uses_exec_form(start, cmd):
    assert f"CMD {cmd}" in file(plan_for("node", push=False, start_command=start), "Dockerfile")


def test_ecs_starts_with_no_tasks_until_an_image_is_pushed():
    t = template(plan_for("python", ecs=True))
    service = t["Resources"]["Service"]["Properties"]
    assert service["DesiredCount"] == {"Fn::If": ["HasImage", 1, 0]}
    assert service["DeploymentConfiguration"]["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}
    task = t["Resources"]["TaskDefinition"]["Properties"]
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    assert task["ContainerDefinitions"][0]["HealthCheck"]["Command"][0] == "CMD"
    ingress = t["Resources"]["ServiceSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert [(r["FromPort"], r["ToPort"]) for r in ingress] == [(3000, 3000)]
    assert "NatGateway" not in str(t["Resources"])


def test_deploy_role_trusts_one_repo_environment_and_is_scoped():
    for ecs in (False, True):
        role = template(plan_for("node", ecs=ecs))["Resources"]["GitHubDeployRole"]
        trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
        assert trust["token.actions.githubusercontent.com:sub"] == {
            "Fn::Sub": "repo:${GitHubRepository}:environment:production"}
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        wildcard = sorted(a for s in statements if s["Resource"] == "*"
                          for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))
        assert wildcard == (["ecr:GetAuthorizationToken", "ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition",
                             "ecs:RegisterTaskDefinition"] if ecs else ["ecr:GetAuthorizationToken"])


def test_ecs_without_ecr_still_pushes_with_a_note():
    plan = plan_for("go", push=False, ecs=True)
    assert ct.WORKFLOW_PATH in [f.path for f in plan.files]
    assert any("needs the image in ECR" in n for n in plan.notes)


def test_bad_inputs_fall_back_with_a_note():
    plan = plan_for("node", app_port="web", region="moon-1")
    notes = " ".join(plan.notes)
    assert "used 8080" in notes and "isn't an AWS region" in notes
    assert "EXPOSE 8080" in file(plan, "Dockerfile")


def test_other_languages_have_no_generator():
    assert plan_for("other") is None


def test_dockerignore_keeps_secrets_and_dependencies_out():
    ignore = file(plan_for("node", push=False), ".dockerignore").splitlines()
    assert {".env", ".git", "node_modules"} <= set(ignore)


def test_cost_note_only_for_paid_parts():
    assert plan_for("node", push=False).estimated_cost_note is None
    assert "$11/month" in plan_for("node", ecs=True).estimated_cost_note


@pytest.mark.skipif(not os.environ.get("INFRAPILOT_VALIDATOR_URL"), reason="validator image not running")
@pytest.mark.parametrize("language,push,ecs", CASES)
def test_every_combination_passes_every_linter_in_the_image(language, push, ecs):
    plan = plan_for(language, push, ecs)
    req = urllib.request.Request(os.environ["INFRAPILOT_VALIDATOR_URL"],
                                 json.dumps({"files": [f.model_dump() for f in plan.files]}).encode())
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    assert all(v["status"] == "pass" for v in body["validations"]), body
    assert {"hadolint", "checkov"} <= {v["tool"] for v in body["validations"]}
