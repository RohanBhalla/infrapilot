"""Embed chunks with Amazon Titan Text Embeddings V2 into a local LanceDB table.

This is the local stand-in for Bedrock Knowledge Bases + S3 Vectors. It uses the same
embedding model, so local retrieval results match what the cloud KB will return.
Embeddings are cached by chunk content hash; unchanged chunks are never re-embedded.

Needs the `embed` extra (boto3, lancedb) and AWS credentials with Bedrock model access.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .config import SETTINGS
from .models import Chunk

log = logging.getLogger(__name__)
TABLE = "chunks"


def load_chunks() -> list[Chunk]:
    path = SETTINGS.paths.chunks / "chunks.jsonl"
    with path.open() as f:
        return [Chunk.model_validate_json(line) for line in f if line.strip()]


def estimate(chunks: list[Chunk]) -> dict:
    emb = SETTINGS.embedding
    tokens = sum(c.token_count for c in chunks)
    return {
        "chunks": len(chunks),
        "tokens": tokens,
        "model": emb.model_id,
        "usd": round(tokens / 1e6 * emb.price_per_million_tokens, 4),
    }


class TitanEmbedder:
    def __init__(self):
        import boto3
        from botocore.config import Config

        emb = SETTINGS.embedding
        self.model_id, self.dimensions = emb.model_id, emb.dimensions
        self.client = boto3.client(
            "bedrock-runtime", region_name=emb.region,
            config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
        )

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": self.dimensions, "normalize": True})
        resp = self.client.invoke_model(modelId=self.model_id, body=body,
                                        contentType="application/json", accept="application/json")
        return json.loads(resp["body"].read())["embedding"]


def _rows(chunks: list[Chunk], vectors: dict[str, list[float]]) -> list[dict]:
    return [
        {
            "chunk_id": c.chunk_id, "doc_id": c.doc_id, "source_id": c.source_id,
            "category": c.category, "doc_type": c.doc_type, "title": c.title, "url": c.url,
            "heading_path": " > ".join(c.heading_path), "chunk_index": c.chunk_index,
            "content_hash": c.content_hash, "text": c.text, "vector": vectors[c.content_hash],
        }
        for c in chunks if c.content_hash in vectors
    ]


def build_text_index(table) -> None:
    """Full-text (keyword) index on chunk text, for hybrid search in the RAG service.
    Overwriting the table drops it, so it's rebuilt after every embed run (~1 s)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # newer API isn't stable in lancedb 0.40
        table.create_fts_index("text", replace=True)


def run_embed(limit: int | None = None, workers: int = 4, batch_size: int = 500) -> dict:
    """Embed new/changed chunks, saving the table after every batch so a crash loses little."""
    import lancedb

    chunks = load_chunks()
    db = lancedb.connect(str(SETTINGS.paths.vectors))
    existing: dict[str, list[float]] = {}
    if TABLE in db.table_names():
        for row in db.open_table(TABLE).to_arrow().select(["content_hash", "vector"]).to_pylist():
            existing[row["content_hash"]] = row["vector"]

    todo = [c for c in chunks if c.content_hash not in existing]
    if limit is not None:
        todo = todo[:limit]
    log.info("%d chunks total, %d cached, embedding %d", len(chunks), len(existing), len(todo))

    embedder = TitanEmbedder()

    def embed_one(c: Chunk) -> list[float] | None:
        try:
            return embedder.embed(c.text)
        except Exception as exc:
            log.warning("embed failed for %s: %s", c.chunk_id, exc)
            return None

    started, failed = time.monotonic(), []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            for c, vec in zip(batch, pool.map(embed_one, batch)):
                if vec is None:
                    failed.append(c.chunk_id)
                else:
                    existing[c.content_hash] = vec
            db.create_table(TABLE, data=_rows(chunks, existing), mode="overwrite")
            log.info("embedded %d/%d (%.0fs)", min(i + batch_size, len(todo)), len(todo),
                     time.monotonic() - started)

    rows = _rows(chunks, existing)
    if not todo:
        db.create_table(TABLE, data=rows, mode="overwrite")  # drop vectors for removed chunks
    build_text_index(db.open_table(TABLE))
    return {**estimate(todo), "embedded_now": len(todo) - len(failed), "failed": failed[:20],
            "failed_count": len(failed), "table_rows": len(rows),
            "seconds": round(time.monotonic() - started, 1)}


def search(query: str, k: int = 5, category: str | None = None) -> list[dict]:
    import lancedb

    table = lancedb.connect(str(SETTINGS.paths.vectors)).open_table(TABLE)
    q = table.search(TitanEmbedder().embed(query)).limit(k)
    if category:
        q = q.where(f"category = '{category.replace(chr(39), '')}'", prefilter=True)
    return [
        {"score": round(1 - r["_distance"] / 2, 4), "title": r["title"], "url": r["url"],
         "section": r["heading_path"], "text": r["text"][:300]}
        for r in q.to_list()
    ]
