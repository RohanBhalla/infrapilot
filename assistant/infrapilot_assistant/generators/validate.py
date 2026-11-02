"""Run linters on generated files when they're installed. Missing tools are reported as
"skipped", not failures, so the chat still works where they aren't bundled (the chat Lambda,
until the validation loop C11 runs them in a container)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..plan import PlanFile, Validation


def _find(tool: str) -> str | None:
    return shutil.which(tool) or next(
        (str(p) for p in [Path(sys.prefix) / "bin" / tool] if p.exists()), None)


def actionlint(files: list[PlanFile]) -> Validation:
    exe = _find("actionlint")
    workflows = [f for f in files if f.path.startswith(".github/workflows/")]
    if not exe or not workflows:
        return Validation(tool="actionlint", status="skipped",
                          findings=[] if workflows else ["no workflow files"])
    with tempfile.TemporaryDirectory() as tmp:
        for f in workflows:
            p = Path(tmp) / f.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f.content)
        r = subprocess.run([exe, "-no-color", "-format", "{{json .}}"] + [f.path for f in workflows],
                           cwd=tmp, capture_output=True, text=True, timeout=30)
    try:
        errors = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return Validation(tool="actionlint", status="fail", findings=[(r.stdout or r.stderr)[:500]])
    findings = [f"{e['filepath']}:{e['line']}:{e['column']}: {e['message']} [{e['kind']}]" for e in errors]
    return Validation(tool="actionlint", status="fail" if findings else "pass", findings=findings)
