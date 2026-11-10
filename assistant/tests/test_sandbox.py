"""Sandbox bundles (decision 004): every deployable generator output becomes a template that only
creates allowlisted, ip-sbx-* resources with the boundary on every role; the rest is refused."""

from __future__ import annotations

import json

import pytest

from infrapilot_assistant import sandbox
from infrapilot_assistant.dialog import _resolved
from infrapilot_assistant.generators import generate, lambda_function as lf
from infrapilot_assistant.intents import INTENTS
from infrapilot_assistant.plan import Validation

from test_github_actions import VERSIONS

BOUNDARY = "arn:aws:iam::123456789012:policy/InfraPilotSandbox-Boundary"
RID = "abc123def4"


def plan_for(intent, **kw):
    plan = generate(intent, _resolved(INTENTS[intent], kw, VERSIONS), VERSIONS)
    plan.validations = []  # the linters are tested elsewhere
    return plan


def bundle(intent, **kw):
    return sandbox.prepare(plan_for(intent, **kw), RID, BOUNDARY)


DEPLOYABLE = [("deploy_lambda", {"trigger": t, "ci_deploy": ci}) for t in lf.TRIGGERS for ci in (True, False)] + [
    ("deploy_static_site", {"framework": "react", "spa_routing": spa, "ci_deploy": ci})
    for spa in (True, False) for ci in (True, False)] + [
    ("containerize", {"language": "node", "app_port": 3000, "push_to_ecr": True})]


@pytest.mark.parametrize("intent,params", DEPLOYABLE)
def test_deployable_plans_only_use_allowed_types_and_bounded_roles(intent, params):
    b = bundle(intent, **params)
    assert b.stack_name == f"ip-sbx-{RID}"
    resources = b.template["Resources"].values()
    assert {r["Type"] for r in resources} <= sandbox.RESOURCE_TYPES
    roles = [r for r in resources if r["Type"] == "AWS::IAM::Role"]
    assert all(r["Properties"]["PermissionsBoundary"] == BOUNDARY for r in roles)
    assert roles or intent == "deploy_static_site"  # a site without CI has no roles
    # No resource names its own role, function or log group outside the stack-name prefix.
    for r in resources:
        for key in ("RoleName", "FunctionName", "LogGroupName", "RepositoryName"):
            assert key not in r.get("Properties", {}) or r["Type"] == "AWS::Lambda::Permission"
    assert len(b.template_body()) <= sandbox.MAX_TEMPLATE_BYTES
    json.loads(b.template_body())


def test_upload_bucket_moves_into_the_sandbox_prefix_everywhere():
    b = bundle("deploy_lambda", trigger="s3_event")
    body = b.template_body()
    assert f'"{lf.UPLOAD_BUCKET_PREFIX}"' not in body
    assert body.count(f'"ip-sbx-{RID}"') == 4  # bucket name, read and write policies, invoke permission
    name = b.template["Resources"]["UploadBucket"]["Properties"]["BucketName"]["Fn::Join"][1][0]
    assert name == f"ip-sbx-{RID}"


def test_short_form_tags_become_json_functions():
    t = bundle("deploy_static_site", framework="plain", ci_deploy=True).template
    assert t["Conditions"]["HasGitHubRepository"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "GitHubRepository"}, ""]}]}
    assert t["Outputs"]["CloudFrontUrl"]["Value"] == {"Fn::GetAtt": ["Distribution", "DomainName"]}
    assert t["Parameters"]["GitHubRepository"]["Default"] == ""  # so no GitHub deploy role is created


def test_static_sites_get_a_sample_page():
    b = bundle("deploy_static_site", framework="react")
    assert b.upload_to == "BucketName" and "<h1>" in b.uploads["index.html"]
    assert bundle("deploy_lambda", trigger="manual").uploads == {}


@pytest.mark.parametrize("intent,params,reason", [
    ("deploy_static_site", {"custom_domain": "example.com"}, "CertificateArn"),
    ("containerize", {"language": "go", "app_port": 8080, "deploy_to_ecs": True}, "EC2 and ECS"),
    ("containerize", {"language": "go", "app_port": 8080, "push_to_ecr": False}, "no AWS template"),
    ("cicd_github_actions", {"language": "node"}, "no AWS template"),
])
def test_plans_the_sandbox_cant_run_are_refused_with_a_reason(intent, params, reason):
    with pytest.raises(sandbox.NotDeployable, match=reason):
        bundle(intent, **params)


def test_failed_plans_are_refused():
    plan = plan_for("deploy_lambda", trigger="manual")
    plan.validations = [Validation(tool="cfn-lint", status="fail", findings=["E3001"])]
    with pytest.raises(sandbox.NotDeployable, match="failed a check"):
        sandbox.prepare(plan, RID, BOUNDARY)


@pytest.mark.parametrize("rid", ["", "ABCDEFGHIJ", "abc", "abcdefghij/x", "abcdefghijk"])
def test_request_ids_are_checked(rid):
    with pytest.raises(ValueError):
        sandbox.stack_name(rid)
