"""Query embeddings with Titan Text Embeddings V2: the same model and settings used to
embed the chunks, so query and chunk vectors are comparable."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Protocol

from .config import CONFIG


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class TitanEmbedder:
    def __init__(self, cfg=CONFIG):
        import boto3
        from botocore.config import Config

        self.model_id, self.dimensions = cfg.embed_model_id, cfg.dimensions
        self.client = boto3.client("bedrock-runtime", region_name=cfg.region,
                                   config=Config(retries={"max_attempts": 6, "mode": "adaptive"}))
        self.embed = lru_cache(maxsize=1024)(self._embed)  # eval runs repeat questions

    def _embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": self.dimensions, "normalize": True})
        resp = self.client.invoke_model(modelId=self.model_id, body=body,
                                        contentType="application/json", accept="application/json")
        return json.loads(resp["body"].read())["embedding"]
