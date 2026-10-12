"""Second-stage reranking with the Bedrock Rerank API.

Search returns ~50 candidates cheaply; a rerank model then reads each candidate next to the
question and reorders them. It catches cases where one word changes the meaning, e.g.
"upload a static website ... with the CDK", where plain search prefers generic S3 pages.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import CONFIG, RERANK_PRICES, RagConfig
from .models import RetrievedChunk

log = logging.getLogger(__name__)

# Amazon Rerank 1.0 isn't offered in us-east-1 (checked 2026-10-12).
MODEL_REGIONS = {"amazon.rerank-v1:0": "us-west-2"}


class Reranker(Protocol):
    model_id: str
    cost_per_query: float

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]: ...


class BedrockReranker:
    def __init__(self, model_id: str | None = None, cfg: RagConfig = CONFIG):
        import boto3
        from botocore.config import Config

        self.model_id = model_id or cfg.rerank_model
        self.region = MODEL_REGIONS.get(self.model_id, cfg.region)
        self.model_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{self.model_id}"
        self.cost_per_query = RERANK_PRICES.get(self.model_id, 0.0)
        self.client = boto3.client("bedrock-agent-runtime", region_name=self.region,
                                   config=Config(retries={"max_attempts": 6, "mode": "adaptive"}))

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        if len(chunks) < 2:
            return chunks
        resp = self.client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[{"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": c.text}}}
                     for c in chunks],
            rerankingConfiguration={"type": "BEDROCK_RERANKING_MODEL", "bedrockRerankingConfiguration": {
                "numberOfResults": len(chunks), "modelConfiguration": {"modelArn": self.model_arn}}},
        )
        out = []
        for r in resp["results"]:
            c = chunks[r["index"]]
            out.append(c.model_copy(update={"metadata": {**c.metadata,
                                                         "rerank_score": round(r["relevanceScore"], 4)}}))
        return out


def make_reranker(cfg: RagConfig = CONFIG) -> Reranker | None:
    if not cfg.rerank_model or cfg.rerank_model.lower() == "none":
        return None
    return BedrockReranker(cfg=cfg)


def apply(reranker: Reranker | None, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Rerank, or keep the search order if reranking is off or the call fails."""
    if reranker is None:
        return chunks
    try:
        return reranker.rerank(query, chunks)
    except Exception as exc:  # an answer from the unreranked order beats no answer
        log.warning("rerank failed (%s); using search order", exc)
        return chunks
