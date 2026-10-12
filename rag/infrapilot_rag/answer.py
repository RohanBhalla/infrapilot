"""Answer service (plan, C7): retrieve -> prompt -> Claude -> answer with checked citations."""

from __future__ import annotations

import time

from .config import CONFIG, RagConfig
from .llm import ChatModel, cost
from .models import Answer, Citation, Filters, RetrievedChunk
from .prompt import REFUSAL, SYSTEM_PROMPT, build_user_message, is_refusal, parse_citations
from .retriever import Retriever
from .versions import VersionRegistry


class AnswerService:
    def __init__(self, retriever: Retriever, llm: ChatModel, versions: VersionRegistry,
                 cfg: RagConfig = CONFIG):
        self.retriever, self.llm, self.versions, self.cfg = retriever, llm, versions, cfg

    def ask(self, question: str, filters: Filters | None = None, k: int | None = None,
            retrieved: list[RetrievedChunk] | None = None) -> Answer:
        """`retrieved` lets a caller (the eval) reuse a search it already ran."""
        started = time.monotonic()
        search_cost = getattr(self.retriever, "query_cost_usd", 0.0) if retrieved is None else 0.0
        if retrieved is None:
            retrieved = self.retriever.retrieve(question, filters, k)
        chunks = [c for c in retrieved if c.score >= self.cfg.min_score]
        facts = self.versions.relevant(question, "\n".join(c.text for c in chunks))
        if not chunks and not facts:
            # Nothing relevant: refuse without paying for an LLM call.
            return Answer(question=question, text=REFUSAL, refused=True, cost_usd=search_cost,
                          latency_ms=int((time.monotonic() - started) * 1000))

        text, usage = self.llm.complete(SYSTEM_PROMPT, build_user_message(question, chunks, facts))
        valid, invalid, cited_versions = parse_citations(text, len(chunks))
        return Answer(
            question=question,
            text=text.strip(),
            refused=is_refusal(text),
            citations=[Citation(n=n, title=chunks[n - 1].title, url=chunks[n - 1].url,
                                chunk_id=chunks[n - 1].chunk_id) for n in valid],
            invalid_citations=invalid,
            cited_versions=cited_versions,
            chunks=chunks,
            versions_used=[f["tool"] for f in facts],
            model=self.llm.model_id,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=cost(self.llm.model_id, usage["input_tokens"], usage["output_tokens"]) + search_cost,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
