"""Static site generator: every framework x SPA x custom domain x CI combination must produce a
template that cfn-lint accepts and (with CI) a workflow that passes actionlint and the pin check."""

from __future__ import annotations

import itertools
import os
import json
import urllib.request

import pytest
import yaml

from infrapilot_assistant.generators import generate
from infrapilot_assistant.generators import static_site as ss
from infrapilot_assistant.generators.validate import _find
from infrapilot_assistant.intents import INTENTS
from infrapilot_assistant.dialog import _resolved

from test_github_actions import VERSIONS

FRAMEWORKS = INTENTS["deploy_static_site"].slot("framework").options
CASES = list(itertools.product(FRAMEWORKS, (True, False), ("www.example.com", None), (True, False)))
needs_linters = pytest.mark.skipif(not (_find("cfn-lint") and _find("actionlint")),
                                   reason="cfn-lint or actionlint not installed")


class CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation short-form tags (!Ref, !Sub, ...) as {"Ref": ...} maps."""


def _tag(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        return {name: value.split(".", 1) if suffix == "GetAtt" else value}
    return {name: loader.construct_sequence(node, deep=True)}


CfnLoader.add_multi_constructor("!", _tag)


def params(**kw):
    return _resolved(INTENTS["deploy_static_site"], kw, VERSIONS)


def plan_for(framework, spa, domain, ci):
    return generate("deploy_static_site", params(source_dir="dist", framework=framework, spa_routing=spa,
                                                 custom_domain=domain, ci_deploy=ci), VERSIONS)


@needs_linters
@pytest.mark.parametrize("framework,spa,domain,ci", CASES)
def test_every_combination_is_valid(framework, spa, domain, ci):
    plan = plan_for(framework, spa, domain, ci)
    status = {v.tool: v.status for v in plan.validations}
    assert status["cfn-lint"] == "pass" and "fail" not in status.values(), [v.findings for v in plan.validations]
    assert set(status) == ({"version-pins", "actionlint", "cfn-lint", "checkov"} if ci else {"cfn-lint", "checkov"})
    t = yaml.load(plan.files[0].content, Loader=CfnLoader)
    dist = t["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
    assert ("CustomErrorResponses" in dist) == spa
    assert ("Aliases" in dist) == bool(domain)
    assert ("GitHubDeployRole" in t["Resources"]) == ci
    if ci:
        wf = yaml.safe_load(plan.files[1].content)
        names = [s["name"] for s in wf["jobs"]["deploy"]["steps"]]
        assert ("Build the site" in names) == (framework != "plain")
        assert wf["jobs"]["deploy"]["permissions"]["id-token"] == "write"


def test_bucket_is_private_and_only_cloudfront_reads_it():
    t = yaml.load(plan_for("react", True, None, False).files[0].content, Loader=CfnLoader)
    bucket = t["Resources"]["SiteBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    allow, deny = t["Resources"]["SiteBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert allow["Principal"] == {"Service": "cloudfront.amazonaws.com"} and allow["Action"] == "s3:GetObject"
    assert "AWS:SourceArn" in allow["Condition"]["StringEquals"]
    assert deny["Effect"] == "Deny" and deny["Condition"] == {"Bool": {"aws:SecureTransport": False}}


def test_deploy_role_trusts_one_repo_environment_and_touches_only_this_site():
    t = yaml.load(plan_for("react", True, None, True).files[0].content, Loader=CfnLoader)
    role = t["Resources"]["GitHubDeployRole"]
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    assert trust["token.actions.githubusercontent.com:sub"] == {
        "Fn::Sub": "repo:${GitHubRepository}:environment:production"}
    actions = [s["Action"] for s in role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]]
    assert "*" not in str(actions) and role["Condition"] == "HasGitHubRepository"


def test_skipped_checkov_rules_explain_why():
    t = yaml.load(plan_for("plain", False, None, False).files[0].content, Loader=CfnLoader)
    skips = [s for r in t["Resources"].values() for s in r.get("Metadata", {}).get("checkov", {}).get("skip", [])]
    assert {s["id"] for s in skips} == {"CKV_AWS_18", "CKV_AWS_68", "CKV_AWS_86", "CKV_AWS_174"}
    assert all(len(s["comment"]) > 20 for s in skips)
    with_domain = yaml.load(plan_for("plain", False, "example.com", False).files[0].content, Loader=CfnLoader)
    dist = with_domain["Resources"]["Distribution"]
    assert "CKV_AWS_174" not in str(dist["Metadata"])  # own certificate: TLS 1.2 minimum instead
    assert dist["Properties"]["DistributionConfig"]["ViewerCertificate"]["MinimumProtocolVersion"] == "TLSv1.2_2021"


def test_bad_inputs_fall_back_with_a_note():
    plan = generate("deploy_static_site", params(source_dir="../../etc", custom_domain="my site!",
                                                 region="moon-1", ci_deploy=False), VERSIONS)
    notes = " ".join(plan.notes)
    assert "used `dist`" in notes and "isn't a domain name" in notes and "isn't an AWS region" in notes
    assert "Aliases" not in plan.files[0].content


def test_default_domain_answer_is_not_a_domain():
    plan = plan_for("plain", False, "none (CloudFront URL)", False)
    assert "Aliases" not in plan.files[0].content and not any("domain" in n for n in plan.notes)


def test_setup_has_the_deploy_command_with_parameters():
    plan = plan_for("react", True, "www.example.com", True)
    cmd = next(s for s in plan.setup if "cloudformation deploy" in s)
    assert "CertificateArn=" in cmd and "GitHubRepository=" in cmd and "CAPABILITY_IAM" in cmd
    assert any("us-east-1" in s and "certificate" in s for s in plan.setup)


@pytest.mark.skipif(not os.environ.get("INFRAPILOT_VALIDATOR_URL"), reason="validator image not running")
@pytest.mark.parametrize("framework,spa,domain,ci", CASES)
def test_every_combination_passes_every_linter_in_the_image(framework, spa, domain, ci):
    plan = plan_for(framework, spa, domain, ci)
    req = urllib.request.Request(os.environ["INFRAPILOT_VALIDATOR_URL"],
                                 json.dumps({"files": [f.model_dump() for f in plan.files]}).encode())
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    assert all(v["status"] == "pass" for v in body["validations"]), body
    assert "checkov" in {v["tool"] for v in body["validations"]}
