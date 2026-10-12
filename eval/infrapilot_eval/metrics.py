"""Scoring helpers. Everything here is pure (no network) so it can be unit tested."""

from __future__ import annotations

import re
import statistics
from urllib.parse import urldefrag

CITE = re.compile(r"\[(?:\d+|V)\]")
PIN = re.compile(r"([\w.-]+/[\w.-]+)@v(\d+)\b")


def norm_url(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


def squash(text: str) -> str:
    """Lowercase and collapse spaces/hyphens/underscores so 'least-privilege' matches 'least privilege'."""
    return re.sub(r"[\s\-_]+", " ", text.lower())


def ranks(retrieved_urls: list[str], expected: list[str]) -> int | None:
    """1-based rank of the first retrieved chunk from an expected page, or None."""
    want = {norm_url(u) for u in expected}
    for i, u in enumerate(retrieved_urls, 1):
        if norm_url(u) in want:
            return i
    return None


def fact_recall(text: str, facts: list[str]) -> float | None:
    if not facts:
        return None
    t = squash(text)
    return sum(squash(f) in t for f in facts) / len(facts)


def content_lines(text: str) -> list[str]:
    """Prose lines that should carry a citation: not code, headings or uncited lead-ins."""
    out, in_code = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or len(s) <= 30 or s.startswith("#") or s.startswith("Not covered in the docs"):
            continue
        if s.rstrip("*").endswith(":") and not CITE.search(s):
            continue
        out.append(s)
    return out


def citation_coverage(text: str) -> float | None:
    lines = content_lines(text)
    return sum(bool(CITE.search(ln)) for ln in lines) / len(lines) if lines else None


def stale_pins(text: str, facts: dict[str, dict]) -> list[str]:
    """Action pins like actions/checkout@v4 whose major differs from the registry."""
    out = []
    for tool, major in PIN.findall(text):
        cur = facts.get(tool, {}).get("extra", {}).get("major")
        if cur and cur != f"v{major}":
            out.append(f"{tool}@v{major} (current {cur})")
    return out


def expected_version(fact: dict, field: str) -> tuple[str, re.Pattern]:
    """(display value, pattern an answer must match) for a version question."""
    extra = fact.get("extra", {})
    if field == "major":
        v = extra["major"]
        return v, re.compile(rf"@?{re.escape(v)}(?![\d])", re.I)
    if field == "lts":
        major = fact["lts"].split(".")[0]
        return fact["lts"], re.compile(rf"(?<![\d.]){major}(?:\.\d+)*(?![\d])")
    if field == "latest_minor":
        v = ".".join(fact["latest"].lstrip("v").split("-")[0].split(".")[:2])
        return v, re.compile(rf"(?<![\d.]){re.escape(v)}(?![\d])")
    if field == "latest":
        v = fact["latest"].lstrip("v")
        return v, re.compile(rf"(?<![\d.])v?{re.escape(v)}(?![\d])")
    if field == "runtime_id":
        v = fact["tool"].removeprefix("lambda-runtime-")
        return v, re.compile(re.escape(v), re.I)
    if field == "status":
        v = extra["status"]
        return v, re.compile(r"\bdeprecat" if v == "deprecated" else r"\bsupported\b", re.I)
    raise ValueError(f"unknown version field {field!r}")


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(p) - 1]


def mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None
