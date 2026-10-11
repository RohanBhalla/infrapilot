"""Pipeline stages: fetch -> process (normalize, filter, dedupe, chunk) -> export -> report.

Each stage reads the previous stage's files under data/, so stages can be re-run
independently and every output can be traced back to its raw source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .chunk import chunk_document
from .config import SETTINGS
from .dedup import dedupe
from .fetchers import STRATEGIES, FetchContext
from .http import PoliteClient
from .models import Chunk, FetchResult, NormalizedDoc, RawDoc, SourceSpec, VersionFact, utcnow
from .normalize import normalize, quality_issue, strip_boilerplate
from .text import slugify

log = logging.getLogger(__name__)
P = SETTINGS.paths


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False))


# ---- fetch ---------------------------------------------------------------------

def fetch_source(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    fn = STRATEGIES[spec.strategy]
    try:
        return fn(spec, ctx)
    except Exception as exc:
        log.exception("[%s] fetch failed", spec.id)
        return FetchResult(source_id=spec.id, errors=[f"fatal: {exc}"])


def save_fetch(spec: SourceSpec, result: FetchResult) -> None:
    src_dir = P.raw / spec.id
    if not result.docs and result.errors and src_dir.exists():
        log.warning("[%s] no docs fetched; keeping previous raw snapshot", spec.id)
    else:
        if src_dir.exists():
            shutil.rmtree(src_dir)  # replace the snapshot so removed pages disappear too
        src_dir.mkdir(parents=True)
        for doc in result.docs:
            name = hashlib.sha1(doc.url.encode()).hexdigest()[:16]
            (src_dir / f"{name}.json").write_text(doc.model_dump_json())

    if result.versions:
        registry = _read_json(P.versions / "versions.json", {})
        for fact in result.versions:
            registry[fact.tool] = fact.model_dump(mode="json")
        _write_json(P.versions / "versions.json", dict(sorted(registry.items())))

    log_path = P.reports / "fetch_log.json"
    fetch_log = _read_json(log_path, {})
    fetch_log[spec.id] = {
        "name": spec.name,
        "strategy": spec.strategy,
        "fetched_at": utcnow().isoformat(),
        "docs": len(result.docs),
        "cache_hits": sum(1 for d in result.docs if d.extra.get("cache_hit")),
        "versions": [v.tool for v in result.versions],
        "errors": result.errors[:50],
        "error_count": len(result.errors),
        "skipped_count": len(result.skipped_urls),
        "skipped_sample": result.skipped_urls[:20],
    }
    _write_json(log_path, dict(sorted(fetch_log.items(), key=lambda kv: int(kv[0]))))


def run_fetch(specs: list[SourceSpec], max_pages: int | None = None, workers: int = 6,
              use_cache: bool = True) -> dict[str, FetchResult]:
    client = PoliteClient(use_cache=use_cache)
    ctx = FetchContext(client=client, max_pages_override=max_pages)
    results: dict[str, FetchResult] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_source, s, ctx): s for s in specs}
            for fut in as_completed(futures):
                spec = futures[fut]
                result = fut.result()
                save_fetch(spec, result)
                results[spec.id] = result
                log.info("[%s] %-45s docs=%-4d versions=%-2d errors=%d skipped=%d", spec.id,
                         spec.name[:45], len(result.docs), len(result.versions),
                         len(result.errors), len(result.skipped_urls))
    finally:
        client.close()
    return results


# ---- process -------------------------------------------------------------------

def load_raw(source_id: str) -> list[RawDoc]:
    src_dir = P.raw / source_id
    if not src_dir.exists():
        return []
    return [RawDoc.model_validate_json(p.read_text()) for p in sorted(src_dir.glob("*.json"))]


def run_process(specs: list[SourceSpec]) -> dict:
    # Dedup priority: explicitly listed pages beat crawls (they carry a specific doc_type,
    # e.g. troubleshooting), then sheet order.
    explicit = {"page", "versions", "upload"}
    order = {s.id: (0 if s.strategy in explicit or s.params.get("pages") else 1) * 10_000 + i
             for i, s in enumerate(specs)}
    stats: dict[str, Counter] = defaultdict(Counter)
    boilerplate: dict[str, list[str]] = {}
    candidates: list[NormalizedDoc] = []

    for spec in specs:
        if not spec.enabled:
            continue
        raws = load_raw(spec.id)
        stats[spec.id]["raw"] = len(raws)
        docs = []
        for raw in raws:
            try:
                docs.append(normalize(raw, spec))
            except Exception as exc:
                log.warning("[%s] normalize failed for %s: %s", spec.id, raw.url, exc)
                stats[spec.id]["drop_normalize_error"] += 1
        docs, removed = strip_boilerplate(docs)
        if removed:
            boilerplate[spec.id] = removed
        for d in docs:
            reason = quality_issue(d)
            if reason:
                stats[spec.id][f"drop_{reason}"] += 1
            else:
                candidates.append(d)

    kept, dropped = dedupe(candidates, order)
    by_id = {d.doc_id: d for d in candidates}
    for doc_id, _, reason in dropped:
        stats[by_id[doc_id].source_id][f"drop_dup_{reason}"] += 1

    for path in (P.normalized, P.chunks, P.kb_export):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)

    all_chunks: list[Chunk] = []
    for doc in kept:
        stats[doc.source_id]["docs"] += 1
        stats[doc.source_id]["doc_tokens"] += doc.token_count
        out = P.normalized / doc.source_id
        out.mkdir(exist_ok=True)
        (out / f"{doc.doc_id}.md").write_text(doc.body_markdown)
        _write_json(out / f"{doc.doc_id}.json", doc.model_dump(mode="json", exclude={"body_markdown"}))
        chunks = chunk_document(doc)
        stats[doc.source_id]["chunks"] += len(chunks)
        stats[doc.source_id]["chunk_tokens"] += sum(c.token_count for c in chunks)
        all_chunks.extend(chunks)

    with (P.chunks / "chunks.jsonl").open("w") as f:
        for c in all_chunks:
            f.write(c.model_dump_json() + "\n")
    export_kb(all_chunks)
    _write_json(P.reports / "dedup.json", [
        {"dropped": a, "kept": b, "reason": r} for a, b, r in dropped
    ])
    _write_json(P.reports / "boilerplate.json", boilerplate)
    return {sid: dict(c) for sid, c in stats.items()}


# ---- export --------------------------------------------------------------------

def kb_metadata(c: Chunk) -> dict:
    """Bedrock KB sidecar. Kept small: S3 Vectors caps filterable metadata at ~2 KB per vector."""
    return {"metadataAttributes": {
        "source_id": c.source_id,
        "category": c.category,
        "doc_type": c.doc_type,
        "doc_id": c.doc_id,
        "title": c.title[:150],
        "url": c.url[:400],
        "fetched_at": c.fetched_at.isoformat(),
    }}


def export_kb(chunks: list[Chunk]) -> None:
    """One file per chunk + .metadata.json, for a Bedrock KB data source with chunking NONE."""
    for c in chunks:
        folder = P.kb_export / slugify(c.category, 40)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{c.chunk_id}.md").write_text(c.text)
        meta = json.dumps(kb_metadata(c), ensure_ascii=False)
        if len(meta.encode()) > 2000:
            log.warning("metadata for %s is %d bytes", c.chunk_id, len(meta.encode()))
        (folder / f"{c.chunk_id}.md.metadata.json").write_text(meta)


# ---- report --------------------------------------------------------------------

def build_report(specs: list[SourceSpec], process_stats: dict | None = None) -> dict:
    fetch_log = _read_json(P.reports / "fetch_log.json", {})
    process_stats = process_stats or _read_json(P.reports / "process_stats.json", {})
    versions = _read_json(P.versions / "versions.json", {})
    emb = SETTINGS.embedding

    rows, totals = [], Counter()
    for s in specs:
        f, p = fetch_log.get(s.id, {}), process_stats.get(s.id, {})
        drops = {k: v for k, v in p.items() if k.startswith("drop_")}
        row = {
            "id": s.id, "name": s.name, "category": s.category, "origin": s.origin,
            "strategy": s.strategy, "skip_reason": s.skip_reason,
            "fetched": f.get("docs", 0), "fetch_errors": f.get("error_count", 0),
            "capped": f.get("skipped_count", 0), "kept_docs": p.get("docs", 0),
            "chunks": p.get("chunks", 0), "tokens": p.get("chunk_tokens", 0), "drops": drops,
            "status": ("skipped" if not s.enabled else "not_fetched" if not f
                       else "ok" if p.get("docs") or f.get("versions")
                       else "covered_by_other" if drops and all(k.startswith("drop_dup") for k in drops)
                       else "empty"),
        }
        rows.append(row)
        for k in ("fetched", "kept_docs", "chunks", "tokens", "fetch_errors"):
            totals[k] += row[k]

    by_category = Counter()
    for r in rows:
        by_category[r["category"]] += r["tokens"]
    report = {
        "generated_at": utcnow().isoformat(),
        "totals": dict(totals),
        "embedding_estimate": {
            "model": emb.model_id,
            "dimensions": emb.dimensions,
            "tokens": totals["tokens"],
            "usd_full_embed": round(totals["tokens"] / 1e6 * emb.price_per_million_tokens, 4),
            "vector_storage_gb": round(totals["chunks"] * emb.dimensions * 4 / 1e9, 4),
        },
        "tokens_by_category": dict(by_category.most_common()),
        "versions": versions,
        "sources": rows,
    }
    _write_json(P.reports / "report.json", report)
    (P.reports / "report.md").write_text(render_report_md(report))
    return report


def render_report_md(r: dict) -> str:
    t, e = r["totals"], r["embedding_estimate"]
    lines = [
        "# InfraPilot corpus report", "", f"Generated {r['generated_at']}", "",
        f"- Docs kept: **{t.get('kept_docs', 0)}** of {t.get('fetched', 0)} fetched",
        f"- Chunks: **{t.get('chunks', 0)}**, tokens: **{t.get('tokens', 0):,}**",
        f"- Full embed with `{e['model']}`: **${e['usd_full_embed']}** "
        f"(vectors ≈ {e['vector_storage_gb']} GB at {e['dimensions']} dims)",
        f"- Fetch errors: {t.get('fetch_errors', 0)}", "",
        "## Tokens by category", "", "| Category | Tokens |", "|---|---:|",
        *[f"| {c} | {n:,} |" for c, n in r["tokens_by_category"].items()], "",
        "## Sources", "",
        "| # | Source | Strategy | Status | Fetched | Kept | Chunks | Tokens | Errors | Capped |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in r["sources"]:
        lines.append(f"| {s['id']} | {s['name']} | {s['strategy']} | {s['status']} | {s['fetched']} | "
                     f"{s['kept_docs']} | {s['chunks']} | {s['tokens']:,} | {s['fetch_errors']} | {s['capped']} |")
    lines += ["", "## Version registry", "", "| Tool | Latest | LTS | EOL |", "|---|---|---|---|"]
    for tool, v in r["versions"].items():
        lines.append(f"| {tool} | {v.get('latest') or ''} | {v.get('lts') or ''} | {v.get('eol') or ''} |")
    return "\n".join(lines) + "\n"


def save_process_stats(stats: dict) -> None:
    _write_json(P.reports / "process_stats.json", stats)


def load_versions() -> dict[str, VersionFact]:
    return {k: VersionFact.model_validate(v) for k, v in _read_json(P.versions / "versions.json", {}).items()}
