"""Sandbox bundles (plan C13, decision 004): turn a validated plan into what the request workflow
deploys as the stack `ip-sbx-<request id>` in the team account.

`prepare` does three things to the plan's CloudFormation template, and refuses plans it can't
deploy safely:
- every IAM role gets the sandbox permissions boundary (the stack's service role can't create a
  role without it);
- the one bucket our generators name themselves (the Lambda upload bucket) is renamed into the
  `ip-sbx-` prefix, so every bucket, function, role and log group starts with the stack name;
- every resource type must be on the allowlist, which matches the service role's permissions.
"""

from __future__ import annotations

import json
import re

import yaml
from pydantic import BaseModel, Field

from .generators import lambda_function
from .plan import Plan

STACK_PREFIX = "ip-sbx-"
REQUEST_ID = re.compile(r"[a-z0-9]{10}")
MAX_TEMPLATE_BYTES = 51_200  # CloudFormation's limit for a template sent in the request body

# What the sandbox service role can create (infra/infrapilot_infra/sandbox_stack.py).
RESOURCE_TYPES = {
    "AWS::ApiGatewayV2::Api",
    "AWS::CloudFront::Distribution",
    "AWS::CloudFront::OriginAccessControl",
    "AWS::ECR::Repository",
    "AWS::IAM::Role",
    "AWS::Lambda::Function",
    "AWS::Lambda::Permission",
    "AWS::Lambda::Url",
    "AWS::Logs::LogGroup",
    "AWS::S3::Bucket",
    "AWS::S3::BucketPolicy",
    "AWS::Scheduler::Schedule",
}

SAMPLE_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deployed by InfraPilot</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem">
<h1>Your site is live</h1>
<p>InfraPilot deployed this S3 + CloudFront site as a sandbox. It is deleted automatically after
4 hours. Use the generated workflow to upload your own build to a stack in your account.</p>
</body>
</html>
"""


class NotDeployable(ValueError):
    """The plan can't run in the sandbox; the message says why, in words for the user."""


class Bundle(BaseModel):
    request_id: str
    stack_name: str
    template: dict
    parameters: dict[str, str] = Field(default_factory=dict)
    # Files uploaded after the stack is created, to the bucket named by the `upload_to` output.
    uploads: dict[str, str] = Field(default_factory=dict)
    upload_to: str | None = None

    def template_body(self) -> str:
        return json.dumps(self.template, separators=(",", ":"))


class _CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation short-form tags (!Ref, !Sub, ...) as their JSON form."""


def _tag(loader, suffix, node):
    name = suffix if suffix in ("Ref", "Condition") else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        return {name: value.split(".", 1) if suffix == "GetAtt" else value}
    if isinstance(node, yaml.MappingNode):
        return {name: loader.construct_mapping(node, deep=True)}
    return {name: loader.construct_sequence(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


def stack_name(request_id: str) -> str:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError(f"bad request id {request_id!r}")
    return STACK_PREFIX + request_id


def _template_file(plan: Plan):
    templates = [f for f in plan.files if f.path.endswith((".yaml", ".yml")) and "AWSTemplateFormatVersion" in f.content]
    if not templates:
        raise NotDeployable("This plan has no AWS template to deploy: it's files for your repo. "
                            "Add them to your repo and they run there.")
    return templates[0]


def _rename_upload_bucket(node, name: str):
    """The Lambda upload bucket is named `uploads-<account>-<id>`; in the sandbox it starts with the
    stack name instead. Its name appears in several !Join lists (bucket, role policy, notification)."""
    if isinstance(node, dict):
        join = node.get("Fn::Join")
        if (isinstance(join, list) and len(join) == 2 and join[0] == "-" and isinstance(join[1], list)
                and join[1] and join[1][0] == lambda_function.UPLOAD_BUCKET_PREFIX):
            join[1][0] = name
        for value in node.values():
            _rename_upload_bucket(value, name)
    elif isinstance(node, list):
        for value in node:
            _rename_upload_bucket(value, name)


def prepare(plan: Plan, request_id: str, boundary_arn: str) -> Bundle:
    """The sandbox bundle for a plan, or NotDeployable with the reason."""
    name = stack_name(request_id)
    if not plan.valid:
        raise NotDeployable("This plan failed a check, so it can't be deployed. Ask me to fix it first.")
    template = yaml.load(_template_file(plan).content, Loader=_CfnLoader)
    resources = template.get("Resources") or {}

    unsupported = {r.get("Type", "?") for r in resources.values()} - RESOURCE_TYPES
    if unsupported:
        services = sorted({t.split("::")[1] if t.count("::") == 2 else t for t in unsupported})
        raise NotDeployable("The sandbox can't create " + " and ".join(services) + " resources yet, so this plan "
                            "can only be deployed to your own account with the setup steps.")
    required = [p for p, spec in (template.get("Parameters") or {}).items() if "Default" not in spec]
    if required:
        raise NotDeployable("This plan needs " + ", ".join(required) + " (for example a certificate for a "
                            "custom domain), which the sandbox can't provide. Ask for it without that to try it here.")

    for resource in resources.values():
        if resource.get("Type") == "AWS::IAM::Role":
            resource.setdefault("Properties", {})["PermissionsBoundary"] = boundary_arn
    _rename_upload_bucket(resources, name)

    bundle = Bundle(request_id=request_id, stack_name=name, template=template)
    if plan.intent == "deploy_static_site":
        bundle.uploads = {"index.html": SAMPLE_PAGE}
        bundle.upload_to = "BucketName"
    if len(bundle.template_body().encode()) > MAX_TEMPLATE_BYTES:
        raise NotDeployable("This template is too large for a sandbox deploy.")
    return bundle
