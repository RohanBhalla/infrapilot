"""Structured version facts: Node.js release schedule and endoflife.date products.

Facts go to the version registry. Each strategy also emits a short Markdown summary so the
RAG corpus can cite current versions directly.
"""

from __future__ import annotations

from datetime import date

from ..models import FetchResult, RawDoc, SourceSpec, VersionFact
from .base import FetchContext

NODE_SCHEDULE = "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json"
EOL_API = "https://endoflife.date/api/{product}.json"


def fetch_versions(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    kind = spec.params["kind"]
    if kind == "node_schedule":
        return _node_schedule(spec, ctx)
    if kind == "endoflife":
        products = spec.params.get("products") or [spec.params["product"]]
        result = FetchResult(source_id=spec.id)
        for product in products:
            sub = _endoflife(spec, ctx, product)
            result.docs += sub.docs
            result.versions += sub.versions
            result.errors += sub.errors
        return result
    raise ValueError(f"unknown versions kind: {kind}")


def _node_schedule(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    schedule: dict = ctx.client.get(NODE_SCHEDULE).json()
    today = date.today().isoformat()
    rows = []
    for line, s in schedule.items():
        if s.get("end", "") < str(int(today[:4]) - 1):
            continue  # skip lines that ended more than a year ago
        status = "EOL" if s.get("end", "") < today else ("LTS" if s.get("lts", "9999") <= today else "Current")
        rows.append(f"| {line} | {s.get('start', '')} | {s.get('lts', '-')} | "
                    f"{s.get('maintenance', '-')} | {s.get('end', '')} | {status} |")
        result.versions.append(VersionFact(
            tool=f"nodejs-{line}", eol=s.get("end"), released_at=s.get("start"),
            source_url=NODE_SCHEDULE, extra={"lts_start": s.get("lts"), "status": status,
                                             "maintenance_start": s.get("maintenance")},
        ))
    body = (
        f"# Node.js release schedule (as of {today})\n\n"
        "| Release line | Start | LTS start | Maintenance start | End of life | Status |\n"
        "|---|---|---|---|---|---|\n" + "\n".join(rows)
    )
    result.docs.append(RawDoc(source_id=spec.id, url=NODE_SCHEDULE, title="Node.js release schedule",
                              format="markdown", body=body))
    return result


def _endoflife(spec: SourceSpec, ctx: FetchContext, product: str) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    url = EOL_API.format(product=product)
    r = ctx.client.get(url)
    if r.status != 200:
        result.errors.append(f"{url}: HTTP {r.status}")
        return result
    cycles: list[dict] = r.json()
    today = date.today().isoformat()

    def supported(c: dict) -> bool:
        eol = c.get("eol")
        return eol is False or (isinstance(eol, str) and eol >= today)

    def is_lts(c: dict) -> bool:
        lts = c.get("lts")
        return lts is True or (isinstance(lts, str) and lts <= today)

    active = [c for c in cycles if supported(c)]
    rows = "\n".join(
        f"| {c.get('releaseLabel') or c['cycle']} | {c.get('latest', '-')} | "
        f"{c.get('releaseDate', '-')} | {c.get('support', '-')} | {c.get('eol')} | {is_lts(c)} |"
        for c in active
    )
    if product == "aws-lambda":
        for c in active:
            result.versions.append(VersionFact(
                tool=f"lambda-runtime-{c['cycle']}", eol=str(c.get("eol")),
                released_at=c.get("releaseDate"), source_url=url,
                extra={"label": c.get("releaseLabel"), "deprecation": c.get("support"),
                       "status": "deprecated" if str(c.get("support", "9999")) < today else "supported"},
            ))
    elif active:
        newest = active[0]
        result.versions.append(VersionFact(
            tool=product, latest=str(newest.get("latest") or newest["cycle"]),
            lts=next((str(c.get("latest")) for c in active if is_lts(c)), None),
            eol=str(newest.get("eol")), released_at=newest.get("releaseDate"), source_url=url,
            extra={"supported_cycles": [c["cycle"] for c in active]},
        ))
    body = (
        f"# {product} supported versions (endoflife.date, as of {today})\n\n"
        "| Cycle | Latest | Released | Support / deprecation | End of life | LTS |\n"
        "|---|---|---|---|---|---|\n" + rows
    )
    result.docs.append(RawDoc(source_id=spec.id, url=f"https://endoflife.date/{product}",
                              title=f"{product} supported versions", format="markdown", body=body))
    return result
