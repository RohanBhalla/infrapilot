"""infrapilot-eval: run the RAG eval or validate the question set.

  infrapilot-eval check                      # free: questions vs. corpus and version registry
  infrapilot-eval run --retrieval-only       # ~$0.12 with reranking (free with INFRAPILOT_RERANK_MODEL=none)
  infrapilot-eval run                        # full run with the default answer model (~$0.25)
  infrapilot-eval run --model gpt-5.6-luna --judge --tag luna
  infrapilot-eval rescore eval/reports/<report>.json   # free: re-grade after fixing labels
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from infrapilot_rag.config import CONFIG, DATA_DIR, MODELS


def check() -> int:
    """Every expected URL is in the corpus, every key fact appears in an expected doc,
    and every version question points at a registry entry."""
    from . import metrics as M
    from .run import load_questions

    docs: dict[str, list[str]] = {}
    for meta in (DATA_DIR / "normalized").glob("*/*.json"):
        url = json.loads(meta.read_text())["url"]
        docs.setdefault(M.norm_url(url), []).append(M.squash(meta.with_suffix(".md").read_text()))
    facts = json.loads((DATA_DIR / "versions" / "versions.json").read_text())

    problems, ids, kinds = [], set(), {}
    for q in load_questions():
        if q["id"] in ids:
            problems.append(f"{q['id']}: duplicate id")
        ids.add(q["id"])
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
        if q["kind"] == "answer":
            missing = [u for u in q["expected_urls"] if M.norm_url(u) not in docs]
            texts = [t for u in q["expected_urls"] for t in docs.get(M.norm_url(u), [])]
            absent = [f for f in q.get("key_facts", []) if not any(M.squash(f) in t for t in texts)]
            if missing:
                problems.append(f"{q['id']}: expected URL not in corpus {missing}")
            if absent:
                problems.append(f"{q['id']}: key fact not in any expected doc {absent}")
        elif q["kind"] == "version":
            fact = facts.get(q["version"]["tool"])
            if not fact:
                problems.append(f"{q['id']}: tool {q['version']['tool']} not in version registry")
            else:
                try:
                    M.expected_version(fact, q["version"]["field"])
                except (KeyError, ValueError, AttributeError) as exc:
                    problems.append(f"{q['id']}: can't derive expected version ({exc})")
    print(f"{len(ids)} questions: {kinds}")
    for p in problems:
        print("  ✗", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="infrapilot-eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate eval/rag/questions.jsonl against the corpus")
    rs = sub.add_parser("rescore", help="re-grade a saved report against the current questions")
    rs.add_argument("report", type=Path)
    r = sub.add_parser("run", help="run the RAG eval and write eval/reports/<date>-...")
    r.add_argument("--model", help=f"answer model ({' | '.join(MODELS)}); default {CONFIG.answer_model_id}")
    r.add_argument("--backend", choices=["local", "kb"], default="local")
    r.add_argument("--retrieval-only", action="store_true", help="skip answers: hit@k / MRR only")
    r.add_argument("--judge", action="store_true", help="also score answers with an LLM judge")
    r.add_argument("--judge-model", default="gpt-5.4", help="judge model (default gpt-5.4)")
    r.add_argument("--ids", help="comma-separated question ids")
    r.add_argument("--limit", type=int)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--tag", default="", help="suffix for the report file name")
    args = p.parse_args(argv)

    if args.cmd == "check":
        sys.exit(check())
    if args.cmd == "rescore":
        from .run import rescore

        _, md = rescore(args.report)
        print(md.read_text().split("\n## Failures")[0])
        return

    from infrapilot_rag.answer import AnswerService
    from infrapilot_rag.llm import make_chat
    from infrapilot_rag.reranker import make_reranker
    from infrapilot_rag.retriever import BedrockKBRetriever, LocalLanceRetriever
    from infrapilot_rag.versions import VersionRegistry

    from .run import run

    try:
        reranker = make_reranker()
        retriever = (BedrockKBRetriever(reranker=reranker) if args.backend == "kb"
                     else LocalLanceRetriever(reranker=reranker))
        llm = make_chat(args.model)
        judge_llm = make_chat(args.judge_model) if args.judge else None
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    svc = AnswerService(retriever, llm, VersionRegistry())
    tag = re.sub(r"[^\w.-]+", "-", args.tag)
    summary, path = run(svc, model=llm.model_id, backend=args.backend, judge_llm=judge_llm,
                        retrieval_only=args.retrieval_only,
                        ids=args.ids.split(",") if args.ids else None, limit=args.limit,
                        workers=args.workers, tag=tag)
    print(Path(path).read_text().split("\n## Failures")[0])
    print(f"report: {path}")


if __name__ == "__main__":
    main()
