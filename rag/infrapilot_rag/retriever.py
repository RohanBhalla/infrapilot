"""The Retriever contract (plan, contract 2) and its two backends.

- LocalLanceRetriever: the LanceDB index built by `infrapilot-data embed`. Free, offline
  apart from the query embedding. Used for development and evals. Hybrid by default: meaning
  (vector) search plus keyword (full-text) search, merged with reciprocal rank fusion, so exact
  names like `Fn::Sub` or `CannotPullContainerError` aren't missed.
- BedrockKBRetriever: a Bedrock Knowledge Base on S3 Vectors (Phase 2). Same contract,
  so the answer service doesn't change when we switch.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Protocol

from .config import CONFIG, RagConfig
from . import reranker as rr
from .embedder import Embedder
from .models import FILTER_FIELDS, Filters, RetrievedChunk

log = logging.getLogger(__name__)


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
    """Keep chunks in the given (best-first) order, at most max_per_doc from any one document."""
    out, per_doc = [], {}
    for c in chunks:
        doc = c.metadata.get("doc_id", c.url)
        if per_doc.get(doc, 0) >= max_per_doc:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        out.append(c)
        if len(out) == k:
            break
    return out


def best_passages(chunks: list[RetrievedChunk], k: int, max_per_doc: int) -> list[RetrievedChunk]:
    """Let the (fused) ranking decide which pages get the k slots, then fill each page's
    slots with that page's highest-scoring chunks. Keyword matches are good at finding the
    right page; meaning scores are better at picking the passage within it."""
    slots = diversify(chunks, k, max_per_doc)
    by_doc: dict[str, list[RetrievedChunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.metadata.get("doc_id", c.url), []).append(c)
    for doc in by_doc:
        by_doc[doc].sort(key=lambda c: c.score, reverse=True)
    return [by_doc[c.metadata.get("doc_id", c.url)].pop(0) for c in slots]


def rrf(*ranked: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion: score each row by sum(1 / (k + rank)) across the lists."""
    score: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for lst in ranked:
        for i, r in enumerate(lst, 1):
            score[r["chunk_id"]] = score.get(r["chunk_id"], 0.0) + 1 / (k + i)
            rows.setdefault(r["chunk_id"], r)
    return [rows[c] for c in sorted(score, key=score.get, reverse=True)]


def keyword_query(text: str) -> str:
    """Strip characters the full-text query parser treats as syntax (`Fn::Sub` -> `Fn Sub`)."""
    return " ".join(re.sub(r"[^\w\s.-]", " ", text).split())


def lance_where(filters: Filters) -> str | None:
    clauses = []
    for field, value in filters.items():
        quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in _values(value))
        clauses.append(f"{field} IN ({quoted})")
    return " AND ".join(clauses) or None


class LocalLanceRetriever:
    META = ("doc_id", "source_id", "category", "doc_type", "heading_path", "chunk_index")

    def __init__(self, embedder: Embedder | None = None, cfg: RagConfig = CONFIG,
                 vectors_dir=None, table: str | None = None, hybrid: bool | None = None,
                 reranker: rr.Reranker | None = None):
        import lancedb

        from .embedder import TitanEmbedder

        self.cfg = cfg
        self.embedder = embedder or TitanEmbedder(cfg)
        self.reranker = reranker
        self.query_cost_usd = reranker.cost_per_query if reranker else 0.0
        db = lancedb.connect(str(vectors_dir or cfg.vectors_dir))
        self.table = db.open_table(table or cfg.table)
        self.hybrid = cfg.hybrid if hybrid is None else hybrid
        if self.hybrid and not any(i.index_type == "FTS" for i in self.table.list_indices()):
            log.warning("no full-text index on the chunks table; using vector search only. "
                        "Rebuild it with `infrapilot-data embed --run`.")
            self.hybrid = False

    def retrieve(self, query: str, filters: Filters | None = None,
                 k: int | None = None) -> list[RetrievedChunk]:
        k = k or self.cfg.k
        n = max(self.cfg.candidates, k)
        where = lance_where(check_filters(filters))
        qvec = self.embedder.embed(query)

        vq = self.table.search(qvec).distance_type("cosine").limit(n)
        if where:
            vq = vq.where(where, prefilter=True)
        vector_rows = vq.to_list()
        for r in vector_rows:
            r["_cosine"] = 1 - r["_distance"]

        rows, keyword_ids = vector_rows, set()
        terms = keyword_query(query) if self.hybrid else ""
        if terms:
            kq = self.table.search(terms, query_type="fts").limit(n)
            if where:
                kq = kq.where(where, prefilter=True)
            keyword_rows = kq.to_list()
            keyword_ids = {r["chunk_id"] for r in keyword_rows}
            for r in keyword_rows:  # vectors are normalized, so the dot product is the cosine
                r["_cosine"] = sum(a * b for a, b in zip(qvec, r["vector"]))
            rows = rrf(vector_rows, keyword_rows, k=self.cfg.rrf_k)

        vector_ids = {r["chunk_id"] for r in vector_rows}
        hits = [
            RetrievedChunk(
                chunk_id=r["chunk_id"], text=r["text"], url=r["url"], title=r["title"],
                score=round(r["_cosine"], 4),  # meaning similarity, used by the min_score cut-off
                metadata={**{m: r[m] for m in self.META if m in r},
                          "match": "+".join(x for x, ids in (("vector", vector_ids), ("keyword", keyword_ids))
                                            if r["chunk_id"] in ids)},
            )
            for r in rows
        ]
        if self.reranker:
            hits = rr.apply(self.reranker, query, hits[:self.cfg.rerank_candidates])
        if self.hybrid or self.reranker:
            return best_passages(hits, k, self.cfg.max_per_doc)
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

    def __init__(self, kb_id: str | None = None, cfg: RagConfig = CONFIG,
                 reranker: rr.Reranker | None = None):
        import boto3

        self.cfg = cfg
        self.reranker = reranker
        self.query_cost_usd = reranker.cost_per_query if reranker else 0.0
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
        hits.sort(key=lambda c: c.score, reverse=True)
        if self.reranker:
            return best_passages(rr.apply(self.reranker, query, hits), k, self.cfg.max_per_doc)
        return diversify(hits, k, self.cfg.max_per_doc)
