"""infrapilot-ask: ask a question from the terminal.

  infrapilot-ask "How do I cache npm dependencies in GitHub Actions?"
  infrapilot-ask "..." --category "CI/CD – GitHub Actions" --model nova-pro
  infrapilot-ask "..." --retrieve-only          # show what search finds, no LLM call
"""

from __future__ import annotations

import argparse
import sys

from .config import CONFIG, MODELS


def build_service(backend: str, model: str | None):
    from .answer import AnswerService
    from .llm import make_chat
    from .reranker import make_reranker
    from .retriever import BedrockKBRetriever, LocalLanceRetriever
    from .versions import VersionRegistry

    reranker = make_reranker()
    retriever = (BedrockKBRetriever(reranker=reranker) if backend == "kb"
                 else LocalLanceRetriever(reranker=reranker))
    llm = make_chat(model)
    return AnswerService(retriever, llm, VersionRegistry())


def _filters(args) -> dict:
    return {"category": args.category, "doc_type": args.doc_type, "source_id": args.source}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="infrapilot-ask", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question")
    p.add_argument("--category", action="append", help="filter by category (repeatable)")
    p.add_argument("--doc-type", action="append", help="filter by doc_type (repeatable)")
    p.add_argument("--source", action="append", help="filter by sheet source id (repeatable)")
    p.add_argument("-k", type=int, default=CONFIG.k, help="chunks to put in the prompt")
    p.add_argument("--model", help=f"{' | '.join(MODELS)} or an OpenAI/Bedrock model id "
                                   f"(default {CONFIG.answer_model_id})")
    p.add_argument("--backend", choices=["local", "kb"], default="local")
    p.add_argument("--retrieve-only", action="store_true", help="show retrieved chunks; no LLM call")
    p.add_argument("--show-context", action="store_true", help="also print the retrieved chunks")
    p.add_argument("--json", action="store_true", help="print the full Answer as JSON")
    args = p.parse_args(argv)

    try:
        svc = build_service(args.backend, args.model)
    except RuntimeError as exc:
        sys.exit(f"error: {exc}")
    if args.retrieve_only:
        for i, c in enumerate(svc.retriever.retrieve(args.question, _filters(args), args.k), 1):
            print(f"[{i}] {c.score:.3f}  {c.title}  ({c.metadata.get('category')})\n    {c.url}")
            print("    " + c.text.split("\n\n", 1)[-1][:240].replace("\n", " ") + "\n")
        return

    ans = svc.ask(args.question, _filters(args), args.k)
    if args.json:
        print(ans.model_dump_json(indent=2))
        return
    print(ans.text + "\n")
    if ans.citations:
        print("Sources:")
        for c in ans.citations:
            print(f"  [{c.n}] {c.title}\n      {c.url}")
    if ans.cited_versions:
        print(f"  [V] version registry: {', '.join(ans.versions_used)}")
    if ans.invalid_citations:
        print(f"  ! cited excerpts that don't exist: {ans.invalid_citations}", file=sys.stderr)
    if args.show_context:
        print("\nRetrieved:")
        for i, c in enumerate(ans.chunks, 1):
            print(f"  [{i}] {c.score:.3f} {c.title} — {c.url}")
    print(f"\n— {ans.model or 'no LLM call'} · {ans.input_tokens}+{ans.output_tokens} tokens · "
          f"${ans.cost_usd:.4f} · {ans.latency_ms} ms")


if __name__ == "__main__":
    main()
