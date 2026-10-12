"""The Retriever contract (plan, contract 2) and its two backends.

- LocalLanceRetriever: the LanceDB index built by `infrapilot-data embed`. Free, offline
  apart from the query embedding. Used for development and evals.
- BedrockKBRetriever: a Bedrock Knowledge Base on S3 Vectors (Phase 2). Same contract,
  so the answer service doesn't change when we switch.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Protocol

from .config import CONFIG, RagConfig
from .embedder import Embedder
from .models import FILTER_FIELDS, Filters, RetrievedChunk


class Retriever(Protocol):
    def retrieve(self, query: str, filters: Filters | None = None,
                 k: int | None = None) -> list[RetrievedChunk]: ...


def _values(v: str | list[str]) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def check_filters(filters: Filters | None) -> Filters:
    filters = {k: v for k, v in (filters or {}).items() if v}
    unknown = set(filters) - set(FILTER_FIELDS)
    if unknown:
        raise ValueError(f"unknown filter field(s) {sorted(unknown)}; allowed: {FILTER_FIELDS}")
    return filters


def diversify(chunks: list[RetrievedChunk], k: int, max_per_doc: int) -> list[RetrievedChunk]:
    """Keep the best-scoring chunks, at most max_per_doc from any one document."""
    out, per_doc = [], {}
    for c in sorted(chunks, key=lambda c: c.score, reverse=True):
        doc = c.metadata.get("doc_id", c.url)
        if per_doc.get(doc, 0) >= max_per_doc:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        out.append(c)
        if len(out) == k:
            break
    return out


def lance_where(filters: Filters) -> str | None:
    clauses = []
    for field, value in filters.items():
        quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in _values(value))
        clauses.append(f"{field} IN ({quoted})")
    return " AND ".join(clauses) or None


class LocalLanceRetriever:
    META = ("doc_id", "source_id", "category", "doc_type", "heading_path", "chunk_index")

    def __init__(self, embedder: Embedder | None = None, cfg: RagConfig = CONFIG,
                 vectors_dir=None, table: str | None = None):
        import lancedb

        from .embedder import TitanEmbedder

        self.cfg = cfg
        self.embedder = embedder or TitanEmbedder(cfg)
        db = lancedb.connect(str(vectors_dir or cfg.vectors_dir))
        self.table = db.open_table(table or cfg.table)

    def retrieve(self, query: str, filters: Filters | None = None,
                 k: int | None = None) -> list[RetrievedChunk]:
        k = k or self.cfg.k
        q = (self.table.search(self.embedder.embed(query))
             .distance_type("cosine")
             .limit(max(self.cfg.candidates, k)))
        where = lance_where(check_filters(filters))
        if where:
            q = q.where(where, prefilter=True)
        hits = [
            RetrievedChunk(
                chunk_id=r["chunk_id"], text=r["text"], url=r["url"], title=r["title"],
                score=round(1 - r["_distance"], 4),
                metadata={m: r[m] for m in self.META if m in r},
            )
            for r in q.to_list()
        ]
        return diversify(hits, k, self.cfg.max_per_doc)


def kb_filter(filters: Filters) -> dict | None:
    parts = []
    for field, value in filters.items():
        vals = _values(value)
        parts.append({"equals": {"key": field, "value": vals[0]}} if len(vals) == 1
                     else {"in": {"key": field, "value": vals}})
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"andAll": parts}


class BedrockKBRetriever:
    """Phase 2 backend. Written against the Retrieve API but not yet run against a live KB."""

    def __init__(self, kb_id: str | None = None, cfg: RagConfig = CONFIG):
        import boto3

        self.cfg = cfg
        self.kb_id = kb_id or cfg.kb_id
        if not self.kb_id:
            raise ValueError("set INFRAPILOT_KB_ID or pass kb_id")
        self.client = boto3.client("bedrock-agent-runtime", region_name=cfg.region)

    def retrieve(self, query: str, filters: Filters | None = None,
                 k: int | None = None) -> list[RetrievedChunk]:
        k = k or self.cfg.k
        search = {"numberOfResults": min(max(self.cfg.candidates, k), 100)}
        flt = kb_filter(check_filters(filters))
        if flt:
            search["filter"] = flt
        resp = self.client.retrieve(knowledgeBaseId=self.kb_id, retrievalQuery={"text": query},
                                    retrievalConfiguration={"vectorSearchConfiguration": search})
        hits = []
        for r in resp["retrievalResults"]:
            meta = r.get("metadata", {})
            uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
            hits.append(RetrievedChunk(
                chunk_id=PurePosixPath(uri).name.removesuffix(".md") or meta.get("doc_id", ""),
                text=r["content"]["text"], url=meta.get("url", uri),
                title=meta.get("title", ""), score=round(r.get("score", 0.0), 4), metadata=meta,
            ))
        return diversify(hits, k, self.cfg.max_per_doc)
