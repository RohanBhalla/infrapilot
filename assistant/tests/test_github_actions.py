"""GitHub Actions generator: every language x package manager x deploy target must produce a
workflow that parses, passes actionlint and pins current action versions. No network or cost."""

from __future__ import annotations

import itertools

import pytest
import yaml

from infrapilot_assistant.generators import generate
from infrapilot_assistant.generators import github_actions as gha
from infrapilot_assistant.generators.pins import check_pins
from infrapilot_assistant.generators.validate import _find
from infrapilot_assistant.intents import INTENTS
from infrapilot_assistant.dialog import _resolved


def fact(major):
    return {"extra": {"major": major}, "source_url": f"https://example.com/{major}"}


VERSIONS = {
    "actions/checkout": fact("v7"), "actions/setup-node": fact("v7"), "actions/upload-artifact": fact("v7"),
    "aws-actions/configure-aws-credentials": fact("v6"), "aws-actions/amazon-ecr-login": fact("v2"),
    "aws-actions/amazon-ecs-render-task-definition": fact("v1"),
    "aws-actions/amazon-ecs-deploy-task-definition": fact("v2"),
    "nodejs": {"lts": "24.21.0"},
    "lambda-runtime-python3.14": {"extra": {"status": "supported"}},
    "lambda-runtime-python3.9": {"extra": {"status": "deprecated"}},
    "lambda-runtime-java25": {"extra": {"status": "supported"}},
}
CASES = [(lang, pm, deploy) for lang, pms in [*gha.PMS.items(), ("other", ("",))]
         for pm, deploy in itertools.product(pms, ("none", "s3_static_site", "lambda", "ecs"))]
needs_actionlint = pytest.mark.skipif(_find("actionlint") is None, reason="actionlint not installed")


def params(**kw):
    """Details as the dialog hands them over: user values plus defaults."""
    return _resolved(INTENTS["cicd_github_actions"], kw, VERSIONS)


@needs_actionlint
@pytest.mark.parametrize("language,pm,deploy", CASES)
def test_every_combination_is_valid(language, pm, deploy):
    plan = generate("cicd_github_actions", params(language=language, package_manager=pm or None,
                                                  deploy_target=deploy), VERSIONS)
    status = {v.tool: v.status for v in plan.validations}
    assert status["version-pins"] == status["actionlint"] == "pass" and "fail" not in status.values(), \
        [v.findings for v in plan.validations]
    assert set(status) <= {"version-pins", "actionlint", "checkov"}  # checkov: skipped unless installed
    wf = yaml.safe_load(plan.files[0].content)
    assert set(wf["jobs"]) == ({"ci"} if deploy == "none" else {"ci", "deploy"})
    assert wf["permissions"] == {"contents": "read"}
    if deploy != "none":
        job = wf["jobs"]["deploy"]
        assert job["permissions"]["id-token"] == "write" and "pull_request" not in job["if"].replace("!=", "")
        assert any(s.get("uses", "").startswith("aws-actions/configure-aws-credentials@v6") for s in job["steps"])
        assert any("AWS_ROLE_ARN" in s for s in plan.setup)


def test_node_defaults_use_registry_versions():
    plan = generate("cicd_github_actions", params(language="node"), VERSIONS)
    wf = yaml.safe_load(plan.files[0].content)
    steps = wf["jobs"]["ci"]["steps"]
    assert steps[0]["uses"] == "actions/checkout@v7"
    assert steps[1]["with"] == {"node-version": "24", "cache": "npm"}
    assert [s["name"] for s in steps[2:]] == ["Install dependencies", "Lint", "Test", "Build"]
    assert wf[True]["push"]["branches"] == ["main"] and "pull_request" in wf[True]  # yaml reads `on:` as True


def test_runtime_default_follows_the_language():
    assert params(language="python")["runtime_version"] == "3.14"
    assert params(language="java")["runtime_version"] == "25"
    assert "runtime_version" not in params(language="go")  # setup-go reads go.mod
    go = yaml.safe_load(generate("cicd_github_actions", params(language="go"), VERSIONS).files[0].content)
    assert go["jobs"]["ci"]["steps"][1]["with"] == {"go-version-file": "go.mod"}


def test_steps_are_read_from_free_text():
    assert gha.parse_steps("unit tests and build") == (["test", "build"], [])
    assert gha.parse_steps("eslint, type-check, tests, docs") == (["lint", "typecheck", "test"], ["docs"])
    assert gha.parse_steps(None) == (["lint", "test", "build"], [])


def test_triggers_are_read_from_free_text():
    t = gha.parse_triggers("pull requests and pushes to develop")
    assert (t.push, t.pull_request, t.schedule, t.branches) == (True, True, False, ["develop"])
    nightly = gha.parse_triggers("nightly")
    assert (nightly.push, nightly.pull_request, nightly.schedule) == (False, False, True)
    assert gha.parse_triggers("whenever").push is True


def test_mismatches_and_unknown_steps_are_explained():
    plan = generate("cicd_github_actions", params(language="python", package_manager="npm", steps="test, docs"),
                    VERSIONS)
    assert "cache: pip" in plan.files[0].content
    assert any("isn't a python package manager" in n for n in plan.notes)
    assert any("docs" in n for n in plan.notes)


def test_static_site_deploy_adds_a_build_step_and_uploads_it():
    plan = generate("cicd_github_actions", params(language="node", steps="lint", deploy_target="s3_static_site"),
                    VERSIONS)
    ci = yaml.safe_load(plan.files[0].content)["jobs"]["ci"]["steps"]
    assert [s["name"] for s in ci][-2:] == ["Build", "Save the build for the deploy job"]


def test_scheduled_only_workflow_warns_that_deploy_needs_a_manual_run():
    plan = generate("cicd_github_actions", params(language="node", triggers="nightly", deploy_target="lambda"),
                    VERSIONS)
    assert any("by hand" in n for n in plan.notes)


def test_outdated_pin_fails_the_check():
    v = check_pins("uses: actions/checkout@v4\nuses: actions/setup-node@v7", VERSIONS)
    assert v.status == "fail" and v.findings == ["actions/checkout@v4: current major is v7"]


def test_citations_include_docs_and_release_pages():
    plan = generate("cicd_github_actions", params(language="node", deploy_target="ecs"), VERSIONS)
    urls = [c.url for c in plan.citations]
    assert any("workflow-syntax" in u for u in urls) and any("oidc-in-aws" in u for u in urls)
    assert any("amazon-elastic-container-service" in u for u in urls) and "https://example.com/v7" in urls


def test_tasks_without_a_generator_return_none():
    assert generate("deploy_static_site", {"source_dir": "dist"}, VERSIONS) is None
