"""Action versions for generated workflows, and a check that every `uses:` is current.

Pins come from the version registry (data_collection's `versions.json`). Actions the registry
doesn't track yet fall back to FALLBACK_PINS; add their repos to the sources sheet so they are
refreshed with the rest.
"""

from __future__ import annotations

import re

from ..plan import Validation

# Checked 2026-11-01 against each repo's latest release. Not refreshed automatically.
FALLBACK_PINS = {
    "actions/setup-python": "v7",
    "actions/setup-java": "v6",
    "actions/setup-go": "v7",
    "actions/download-artifact": "v8",
    "pnpm/action-setup": "v6",
    # Checked 2026-11-09.
    "docker/setup-qemu-action": "v4",
    "docker/setup-buildx-action": "v4",
    "docker/build-push-action": "v7",
}

USES = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@(v\d+)")


def major(action: str, versions: dict | None) -> str:
    fact = (versions or {}).get(action)
    if fact and fact.get("extra", {}).get("major"):
        return fact["extra"]["major"]
    if action in FALLBACK_PINS:
        return FALLBACK_PINS[action]
    raise KeyError(f"no version known for {action}")


def pin(action: str, versions: dict | None) -> str:
    return f"{action}@{major(action, versions)}"


def source_url(action: str, versions: dict | None) -> str:
    fact = (versions or {}).get(action)
    return fact["source_url"] if fact and fact.get("source_url") else f"https://github.com/{action}/releases"


def check_pins(text: str, versions: dict | None) -> Validation:
    """Every `uses: owner/repo@vN` must be the current major. Untracked actions are findings too,
    but only fail when the pin is known to be wrong."""
    findings, failed = [], False
    for action, used in sorted(set(USES.findall(text))):
        try:
            current = major(action, versions)
        except KeyError:
            findings.append(f"{action}@{used}: not in the version registry")
            continue
        if used != current:
            failed = True
            findings.append(f"{action}@{used}: current major is {current}")
        elif action not in (versions or {}):
            findings.append(f"{action}@{used}: pinned from the fallback list (not refreshed automatically)")
    return Validation(tool="version-pins", status="fail" if failed else "pass", findings=findings)
