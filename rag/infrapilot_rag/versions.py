"""Version registry (plan, contract 3) and picking the facts relevant to a question.

Doc examples often show old versions (e.g. actions/checkout@v3). The answer prompt gets
current facts from the registry so generated answers pin current versions.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .config import CONFIG

# Topic -> (pattern that detects it in a question, registry tool-name prefixes it pulls in)
TOPICS: dict[str, tuple[re.Pattern, tuple[str, ...]]] = {
    "github_actions": (re.compile(r"github actions?|workflow|\.github|actions/|uses:|\bci\b|pipeline", re.I),
                       ("actions/", "aws-actions/", "docker/", "aquasecurity/", "hadolint/")),
    "node": (re.compile(r"\bnode(?:js|\.js)?\b|\bnpm\b|\byarn\b|\bpnpm\b|setup-node", re.I), ("nodejs",)),
    "lambda": (re.compile(r"\blambda\b", re.I), ("lambda-runtime-",)),
    "kubernetes": (re.compile(r"kubernetes|\bk8s\b|\beks\b|kubectl|karpenter|helm|eksctl", re.I),
                   ("kubernetes", "amazon-eks")),
    "cdk": (re.compile(r"\bcdk\b", re.I), ("aws-cdk",)),
}


class VersionRegistry:
    def __init__(self, path: Path = CONFIG.versions_path, today: date | None = None):
        self.facts: dict[str, dict] = json.loads(Path(path).read_text()) if Path(path).exists() else {}
        self.today = (today or date.today()).isoformat()

    def get(self, tool: str) -> dict | None:
        return self.facts.get(tool)

    def _current(self, fact: dict) -> bool:
        """Drop release lines that are already end-of-life and deprecated runtimes."""
        if fact["tool"].startswith("lambda-runtime-"):
            return fact.get("extra", {}).get("status") == "supported"
        return not (fact.get("eol") and fact["eol"] < self.today and not fact.get("latest"))

    def relevant(self, question: str) -> list[dict]:
        q = question.lower()
        picked = []
        for tool, fact in self.facts.items():
            short = tool.rsplit("/", 1)[-1].removeprefix("lambda-runtime-")
            # Name match only for distinctive names (setup-node, python3.13), not plain
            # words like "cache" that show up in unrelated questions.
            distinctive = tool in q or re.search(r"[-.\d]", short)
            named = bool(distinctive) and re.search(
                rf"(?<![\w-]){re.escape(short)}(?![\w-])", q) is not None
            topical = any(pat.search(question) and tool.startswith(prefixes)
                          for pat, prefixes in TOPICS.values())
            if named or (topical and self._current(fact)):
                picked.append(fact)
        return picked


def format_fact(f: dict) -> str:
    tool, extra = f["tool"], f.get("extra", {})
    if tool.startswith("lambda-runtime-"):
        rid = tool.removeprefix("lambda-runtime-")
        return (f"- Lambda runtime {extra.get('label', rid)} (`{rid}`): {extra.get('status', 'unknown')}"
                + (f", deprecation {extra['deprecation']}" if extra.get("deprecation") else ""))
    if tool == "nodejs":
        return (f"- Node.js: latest {f['latest']}, current LTS {f['lts']}; "
                f"supported release lines {', '.join(extra.get('supported_cycles', []))}")
    if tool.startswith("nodejs-v"):
        return f"- Node.js {tool.removeprefix('nodejs-v')}: {extra.get('status', '')}, end of life {f.get('eol')}"
    if tool in ("kubernetes", "amazon-eks"):
        return (f"- {tool}: latest {f['latest']}; supported versions "
                f"{', '.join(extra.get('supported_cycles', []))}")
    line = f"- {tool}: latest {f['latest']}"
    if extra.get("pin_hint"):
        line += f" (pin as `{extra['pin_hint']}`)"
    return line + (f", released {f['released_at'][:10]}" if f.get("released_at") else "")
