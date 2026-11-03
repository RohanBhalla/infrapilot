"""Checks for generated files (plan C11): actionlint, hadolint, cfn-lint and checkov.

Each linter runs only on the files it understands, and only when it's installed. A missing
linter is reported as "skipped", not a failure, so generation works anywhere.

On AWS the chat Lambda doesn't carry the linters (checkov alone is hundreds of MB). When
INFRAPILOT_VALIDATOR_FUNCTION is set, `run` sends the files to the validator Lambda instead: a
container image with all four installed (validator/Dockerfile), whose handler is
`lambda_handler` below. If it can't be reached, the checks report "skipped" and the chat goes on.

Generators are deterministic templates, so there's no retry loop: a failed check means a bug in
a template, and the tests (every combination through every linter) are where it gets fixed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from ..plan import PlanFile, Validation

log = logging.getLogger(__name__)
TIMEOUT = 20  # seconds per linter
# The chat Lambda has 29 s in all, and the classifier can take 12 s when OpenAI stalls. A cold
# validator (~12 s) may miss this and report "skipped"; it still finishes, so the next call is warm.
REMOTE_TIMEOUT = 12


def _find(tool: str) -> str | None:
    return shutil.which(tool) or next(
        (str(p) for p in [Path(sys.prefix) / "bin" / tool] if p.exists()), None)


def _is_workflow(f: PlanFile) -> bool:
    return f.path.startswith(".github/workflows/") and f.path.endswith((".yml", ".yaml"))


def _is_dockerfile(f: PlanFile) -> bool:
    name = PurePosixPath(f.path).name
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


def _is_cloudformation(f: PlanFile) -> bool:
    return f.path.endswith((".json", ".yml", ".yaml", ".template")) and not _is_workflow(f) and (
        "AWSTemplateFormatVersion" in f.content or ".template." in f.path)


def _write(tmp: str, files: list[PlanFile]) -> None:
    for f in files:
        p = Path(tmp) / f.path
        if not p.resolve().is_relative_to(Path(tmp).resolve()):  # no ../ escapes
            raise ValueError(f"bad file path: {f.path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content)


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT,
                          env={**os.environ, "HOME": os.environ.get("HOME", tempfile.gettempdir())})


def _result(tool: str, findings: list[str]) -> Validation:
    return Validation(tool=tool, status="fail" if findings else "pass", findings=findings)


def _unreadable(tool: str, r: subprocess.CompletedProcess) -> Validation:
    return Validation(tool=tool, status="fail", findings=[((r.stdout or "") + (r.stderr or ""))[:500]])


def actionlint(exe: str, tmp: str, files: list[PlanFile]) -> Validation:
    r = _run([exe, "-no-color", "-format", "{{json .}}"] + [f.path for f in files], tmp)
    try:
        errors = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return _unreadable("actionlint", r)
    return _result("actionlint", [f"{e['filepath']}:{e['line']}:{e['column']}: {e['message']} [{e['kind']}]"
                                  for e in errors])


def hadolint(exe: str, tmp: str, files: list[PlanFile]) -> Validation:
    r = _run([exe, "--no-color", "-f", "json"] + [f.path for f in files], tmp)
    try:
        errors = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return _unreadable("hadolint", r)
    return _result("hadolint", [f"{e['file']}:{e['line']}: {e['message']} [{e['code']}]" for e in errors])


def cfn_lint(exe: str, tmp: str, files: list[PlanFile]) -> Validation:
    r = _run([exe, "--format", "json", "--"] + [f.path for f in files], tmp)
    try:
        errors = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return _unreadable("cfn-lint", r)
    return _result("cfn-lint", [
        f"{e['Filename']}:{e['Location']['Start']['LineNumber']}: {e['Message']} [{e['Rule']['Id']}]"
        for e in errors if e.get("Level") != "Informational"])


CHECKOV_FRAMEWORKS = {"github_actions": _is_workflow, "dockerfile": _is_dockerfile,
                      "cloudformation": _is_cloudformation}


def checkov(exe: str, tmp: str, files: list[PlanFile]) -> Validation:
    frameworks = [name for name, wants in CHECKOV_FRAMEWORKS.items() if any(wants(f) for f in files)]
    # --skip-download: no calls to Prisma Cloud, so no account or key is needed.
    r = _run([exe, "-d", ".", "--framework", *frameworks, "--output", "json", "--quiet", "--compact",
              "--skip-download", "--no-fail-on-crash"], tmp)
    try:
        reports = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return _unreadable("checkov", r)
    if isinstance(reports, dict):  # one framework gives an object, several a list
        reports = [reports]
    failed = [c for rep in reports for c in rep.get("results", {}).get("failed_checks", [])]
    return _result("checkov", sorted(
        f"{c['file_path'].lstrip('/')}:{(c.get('file_line_range') or [0])[0]}: {c['check_name']} [{c['check_id']}]"
        for c in failed))


# tool name, executable, which files it reads, runner
LINTERS: list[tuple[str, str, Callable[[PlanFile], bool], Callable]] = [
    ("actionlint", "actionlint", _is_workflow, actionlint),
    ("hadolint", "hadolint", _is_dockerfile, hadolint),
    ("cfn-lint", "cfn-lint", _is_cloudformation, cfn_lint),
    ("checkov", "checkov", lambda f: any(w(f) for w in CHECKOV_FRAMEWORKS.values()), checkov),
]


def run_local(files: list[PlanFile]) -> list[Validation]:
    """Every linter that applies to these files: run if installed, otherwise "skipped"."""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, files)
        for tool, exe_name, wants, runner in LINTERS:
            mine = [f for f in files if wants(f)]
            if not mine:
                continue
            exe = _find(exe_name)
            if not exe:
                out.append(Validation(tool=tool, status="skipped", findings=["not installed here"]))
                continue
            try:
                out.append(runner(exe, tmp, mine))
            except subprocess.TimeoutExpired:
                out.append(Validation(tool=tool, status="skipped", findings=[f"took over {TIMEOUT} s"]))
    return out


def _applicable(files: list[PlanFile]) -> list[str]:
    return [tool for tool, _, wants, _ in LINTERS if any(wants(f) for f in files)]


def run_remote(files: list[PlanFile], function: str, client=None) -> list[Validation]:
    try:
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("lambda", config=Config(
                connect_timeout=3, read_timeout=REMOTE_TIMEOUT, retries={"max_attempts": 0}))
        r = client.invoke(FunctionName=function,
                          Payload=json.dumps({"files": [f.model_dump() for f in files]}).encode())
        body = json.loads(r["Payload"].read())
        if r.get("FunctionError"):
            raise RuntimeError(body.get("errorMessage", "validator error"))
        return [Validation.model_validate(v) for v in body["validations"]]
    except Exception as exc:
        log.warning("validator unavailable: %s", exc)
        return [Validation(tool=t, status="skipped", findings=["the checker didn't respond"])
                for t in _applicable(files)]


def run(files: list[PlanFile]) -> list[Validation]:
    """Check generated files: on the validator Lambda when configured, otherwise here."""
    function = os.environ.get("INFRAPILOT_VALIDATOR_FUNCTION")
    return run_remote(files, function) if function else run_local(files)


def lambda_handler(event, context):
    """The validator Lambda: {"files": [PlanFile, ...]} -> {"validations": [Validation, ...]}."""
    files = [PlanFile.model_validate(f) for f in event.get("files", [])][:20]
    return {"validations": [v.model_dump() for v in run_local(files)]}
