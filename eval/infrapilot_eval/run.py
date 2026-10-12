"""Run the RAG eval: retrieval metrics, then answers, then (optionally) the judge."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from infrapilot_rag.config import CONFIG
from infrapilot_rag.llm import cost
from infrapilot_rag.prompt import build_user_message

from . import metrics as M
from .judge import judge

EVAL_DIR = Path(__file__).resolve().parents[1]
QUESTIONS = EVAL_DIR / "rag" / "questions.jsonl"
REPORTS = EVAL_DIR / "reports"
RETRIEVAL_K = 10
TARGETS = {"hit@5": 0.8, "citation_accuracy": 0.9, "version_correctness": 1.0, "refusal_accuracy": 0.8}


def load_questions(path: Path = QUESTIONS, ids: list[str] | None = None,
                   limit: int | None = None) -> list[dict]:
    qs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if ids:
        qs = [q for q in qs if q["id"] in ids]
    return qs[:limit] if limit else qs


def evaluate_one(q: dict, svc, judge_llm=None, retrieval_only: bool = False) -> dict:
    r = {"id": q["id"], "kind": q["kind"], "category": q["category"], "question": q["question"]}
    if q["kind"] == "answer":
        hits = svc.retriever.retrieve(q["question"], None, RETRIEVAL_K)
        rank = M.ranks([h.url for h in hits], q["expected_urls"])
        r |= {"rank": rank, "retrieved_urls": [h.url for h in hits], "top_score": hits[0].score if hits else None,
              "search_cost_usd": getattr(svc.retriever, "query_cost_usd", 0.0)}
    if retrieval_only:
        return r

    # Reuse the search above (one rerank call per question, not two). best_passages fills
    # slots in order, so the first k of the k=10 list equal a k-sized search.
    ans = svc.ask(q["question"], retrieved=hits[:CONFIG.k]) if q["kind"] == "answer" else svc.ask(q["question"])
    r |= {"answer": ans.text, "refused": ans.refused, "latency_ms": ans.latency_ms,
          "input_tokens": ans.input_tokens, "output_tokens": ans.output_tokens, "cost_usd": ans.cost_usd,
          "cited_urls": [c.url for c in ans.citations], "invalid_citations": ans.invalid_citations,
          "coverage": M.citation_coverage(ans.text) if not ans.refused else None,
          "stale_pins": M.stale_pins(ans.text, svc.versions.facts)}
    if q["kind"] == "answer" and not ans.refused:
        want = {M.norm_url(u) for u in q["expected_urls"]}
        r["expected_cited"] = any(M.norm_url(u) in want for u in r["cited_urls"])
        r["citation_ok"] = r["expected_cited"] and not ans.invalid_citations
        r["fact_recall"] = M.fact_recall(ans.text, q.get("key_facts", []))
    if q["kind"] == "version":
        fact = svc.versions.get(q["version"]["tool"])
        value, pattern = M.expected_version(fact, q["version"]["field"])
        r["expected_version"] = value
        r["version_ok"] = (not ans.refused) and bool(pattern.search(ans.text))
    if judge_llm and q["kind"] == "answer" and not ans.refused:
        facts = svc.versions.relevant(q["question"], "\n".join(c.text for c in ans.chunks))
        user = build_user_message(q["question"], ans.chunks, facts)
        j = judge(judge_llm, q["question"], user, ans.text)
        u = j.pop("usage")
        r["judge"] = j
        r["judge_cost_usd"] = cost(judge_llm.model_id, u["input_tokens"], u["output_tokens"])
    return r


def rescore(report: Path) -> tuple[dict, Path]:
    """Re-grade a saved report against the current questions.jsonl (labels fixed) for free."""
    data = json.loads(report.read_text())
    qs = {q["id"]: q for q in load_questions()}
    for r in data["rows"]:
        q = qs.get(r["id"])
        if not q or q["kind"] != "answer":
            continue
        urls = r.get("retrieved_urls") or r.get("top_urls") or []
        new_rank = M.ranks(urls, q["expected_urls"])
        if new_rank and (r["rank"] is None or new_rank < r["rank"]):
            r["rank"] = new_rank
        if "cited_urls" in r and not r.get("refused"):
            want = {M.norm_url(u) for u in q["expected_urls"]}
            r["expected_cited"] = any(M.norm_url(u) in want for u in r["cited_urls"])
            r["citation_ok"] = r["expected_cited"] and not r["invalid_citations"]
            r["fact_recall"] = M.fact_recall(r["answer"], q.get("key_facts", []))
    data["summary"] = summarize(data["rows"])
    data["meta"]["rescored"] = True
    report.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    md = report.with_suffix(".md")
    md.write_text(render(data["meta"], data["summary"], data["rows"]))
    return data["summary"], md


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["kind"] == "answer"]
    ver = [r for r in rows if r["kind"] == "version"]
    ref = [r for r in rows if r["kind"] == "refuse"]
    answered = [r for r in ans if "answer" in r and not r["refused"]]
    lat = sorted(r["latency_ms"] for r in rows if "latency_ms" in r and not r.get("refused"))
    s = {
        "questions": len(rows),
        "hit@1": M.mean([r["rank"] == 1 for r in ans]),
        "hit@5": M.mean([r["rank"] is not None and r["rank"] <= 5 for r in ans]),
        "mrr@10": M.mean([1 / r["rank"] if r["rank"] else 0 for r in ans]),
    }
    if any("answer" in r for r in rows):
        s |= {
            "citation_accuracy": M.mean([r["citation_ok"] for r in answered]),
            "citation_coverage": M.mean([r["coverage"] for r in answered]),
            "invalid_citations": sum(len(r["invalid_citations"]) for r in rows if "answer" in r),
            "key_fact_recall": M.mean([r["fact_recall"] for r in answered]),
            "version_correctness": M.mean([r["version_ok"] for r in ver]),
            "refusal_accuracy": M.mean([r["refused"] for r in ref]),
            "false_refusals": sum(r["refused"] for r in ans + ver),
            "stale_pins": sum(len(r["stale_pins"]) for r in rows if "answer" in r),
            "latency_p50_ms": M.percentile(lat, 50),
            "latency_p95_ms": M.percentile(lat, 95),
            "cost_per_answer_usd": M.mean([r["cost_usd"] for r in rows if "cost_usd" in r and r["input_tokens"]]),
        }
        judged = [r["judge"] for r in answered if r.get("judge")]
        if judged:
            s["judge_faithfulness"] = M.mean([j["faithfulness"] for j in judged])
            s["judge_correctness"] = M.mean([j["correctness"] for j in judged])
    s["total_cost_usd"] = sum(r.get("cost_usd", 0) + r.get("judge_cost_usd", 0) + r.get("search_cost_usd", 0)
                              for r in rows)
    by_cat: dict[str, list] = {}
    for r in ans:
        by_cat.setdefault(r["category"], []).append(r["rank"] is not None and r["rank"] <= 5)
    s["hit@5_by_category"] = {c: (sum(v) / len(v), len(v)) for c, v in sorted(by_cat.items())}
    return s


def run(svc, *, model: str, backend: str, judge_llm=None, retrieval_only=False, ids=None,
        limit=None, workers: int = 4, tag: str = "") -> tuple[dict, Path]:
    qs = load_questions(ids=ids, limit=limit)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda q: evaluate_one(q, svc, judge_llm, retrieval_only), qs))
    summary = summarize(rows)
    meta = {"date": date.today().isoformat(), "backend": backend,
            "model": None if retrieval_only else model, "judge": getattr(judge_llm, "model_id", None),
            "retrieval": {"k": CONFIG.k, "candidates": CONFIG.candidates, "max_per_doc": CONFIG.max_per_doc,
                          "min_score": CONFIG.min_score, "hybrid": getattr(svc.retriever, "hybrid", None),
                          "rerank": getattr(getattr(svc.retriever, "reranker", None), "model_id", None)},
            "seconds": round(time.monotonic() - started, 1)}
    name = "-".join(x for x in [meta["date"], backend, "retrieval" if retrieval_only else model, tag] if x)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{name}.json").write_text(json.dumps({"meta": meta, "summary": summary, "rows": rows},
                                                       indent=2, ensure_ascii=False))
    path = REPORTS / f"{name}.md"
    path.write_text(render(meta, summary, rows))
    return summary, path


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.3f}" if v < 10 else f"{v:,.0f}"
    return str(v)


def render(meta: dict, s: dict, rows: list[dict]) -> str:
    lines = [f"# RAG eval: {meta['date']} · {meta['backend']} · {meta['model'] or 'retrieval only'}", "",
             f"Judge: {meta['judge'] or 'off'} · retrieval {meta['retrieval']} · {meta['seconds']} s", "",
             "| Metric | Value | Target |", "|---|---:|---:|"]
    for k, v in s.items():
        if k == "hit@5_by_category":
            continue
        t = TARGETS.get(k)
        mark = "" if t is None or v is None else (" ✅" if v >= t else " ❌")
        lines.append(f"| {k} | {_fmt(v)}{mark} | {_fmt(t) if t else ''} |")
    lines += ["", "## hit@5 by category", "", "| Category | hit@5 | n |", "|---|---:|---:|"]
    lines += [f"| {c} | {v:.2f} | {n} |" for c, (v, n) in s["hit@5_by_category"].items()]

    fails = []
    for r in rows:
        why = []
        if r["kind"] == "answer" and not (r["rank"] and r["rank"] <= 5):
            why.append(f"retrieval miss (rank {r['rank']})")
        if r.get("refused") and r["kind"] != "refuse":
            why.append("wrongly refused")
        if r["kind"] == "refuse" and "refused" in r and not r["refused"]:
            why.append("answered instead of refusing")
        if r.get("citation_ok") is False:
            why.append("expected source not cited" if not r.get("expected_cited") else "invalid citation")
        if r.get("version_ok") is False:
            why.append(f"version wrong (expected {r['expected_version']})")
        if r.get("stale_pins"):
            why.append(f"stale pins {r['stale_pins']}")
        if why:
            fails.append(f"| {r['id']} | {'; '.join(why)} |")
    lines += ["", f"## Failures ({len(fails)})", "", "| Question | Problem |", "|---|---|", *fails]
    return "\n".join(lines) + "\n"
