import ast
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infrapilot_infra.sandbox_stack import SandboxStack


@pytest.fixture(scope="module")
def t():
    app = cdk.App()
    stack = SandboxStack(app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


def statements(t, description):
    policy = next(p for p in t.find_resources("AWS::IAM::ManagedPolicy").values()
                  if p["Properties"]["Description"].startswith(description))
    return policy["Properties"]["PolicyDocument"]["Statement"]


def as_list(x):
    return x if isinstance(x, list) else [x]


def test_only_cloudformation_can_use_the_service_role(t):
    t.has_resource_properties("AWS::IAM::Role", {"AssumeRolePolicyDocument": {"Statement": [Match.object_like({
        "Principal": {"Service": "cloudformation.amazonaws.com"},
        "Condition": {"StringEquals": {"aws:SourceAccount": "123456789012"}}})], "Version": "2012-10-17"}})


def test_roles_can_only_be_created_with_the_boundary(t):
    s = statements(t, "InfraPilot sandbox: what CloudFormation")
    for st in s:
        if st["Effect"] == "Allow" and {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"} & set(as_list(st["Action"])):
            assert set(st["Condition"]["StringEquals"]) == {"iam:PermissionsBoundary"}
    iam_resources = [r for st in s if any(a.startswith("iam:") for a in as_list(st["Action"])) for r in as_list(st["Resource"])]
    assert iam_resources and all("role/ip-sbx-*" in str(r) for r in iam_resources)


def test_service_role_never_touches_persistent_resources(t):
    deny = [st for st in statements(t, "InfraPilot sandbox: what CloudFormation") if st["Effect"] == "Deny"]
    assert deny == [{"Effect": "Deny", "Action": "*", "Resource": "*",
                     "Condition": {"StringEquals": {"aws:ResourceTag/lifecycle": "persistent"}}}]


def test_named_services_are_limited_to_the_sandbox_prefix(t):
    for st in statements(t, "InfraPilot sandbox: what CloudFormation"):
        actions = as_list(st["Action"])
        if st["Effect"] == "Allow" and any(a.split(":")[0] in ("s3", "lambda", "scheduler", "ecr") for a in actions):
            assert all("ip-sbx-*" in str(r) for r in as_list(st["Resource"])), st


def test_boundary_has_no_iam_and_no_wildcards_beyond_ecr_login(t):
    s = statements(t, "InfraPilot sandbox: the most")
    actions = [a for st in s for a in as_list(st["Action"])]
    assert not any(a.startswith("iam:") or a.endswith(":*") for a in actions)
    assert [st["Action"] for st in s if st["Resource"] == "*"] == ["ecr:GetAuthorizationToken"]


def sandbox_resource_types():
    """RESOURCE_TYPES from the assistant's sandbox.py, read without importing it (other venv)."""
    tree = ast.parse((Path(__file__).parents[2] / "assistant/infrapilot_assistant/sandbox.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "RESOURCE_TYPES")
    return ast.literal_eval(node.value)


def test_every_sandbox_resource_type_has_a_permission(t):
    services = {"ApiGatewayV2": "apigateway", "CloudFront": "cloudfront", "ECR": "ecr", "IAM": "iam",
                "Lambda": "lambda", "Logs": "logs", "S3": "s3", "Scheduler": "scheduler"}
    s = statements(t, "InfraPilot sandbox: what CloudFormation")
    allowed = {a.split(":")[0] for st in s if st["Effect"] == "Allow" for a in as_list(st["Action"])}
    assert {services[kind.split("::")[1]] for kind in sandbox_resource_types()} <= allowed
