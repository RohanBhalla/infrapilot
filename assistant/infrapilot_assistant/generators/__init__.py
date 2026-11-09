"""File generators (plan C10): one per task intent, each returning a `Plan`."""

from __future__ import annotations

from typing import Callable

from ..plan import Plan
from . import github_actions, lambda_function, static_site

GENERATORS: dict[str, Callable[[dict, dict | None], Plan]] = {
    "cicd_github_actions": github_actions.generate,
    "deploy_lambda": lambda_function.generate,
    "deploy_static_site": static_site.generate,
}


def generate(intent: str, params: dict, versions: dict | None = None) -> Plan | None:
    """The plan for a ready task, or None when that task has no generator yet."""
    gen = GENERATORS.get(intent)
    return gen(params, versions) if gen else None
