"""Paths and tunables shared by every pipeline stage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # data_collection/

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1qhb5pxeY7RZiWZd4Ey7J-nUNlBPjoQn1SwntbEyert8"
    "/export?format=csv"
)

USER_AGENT = "InfraPilotDocBot/0.1 (+https://github.com/RohanBhalla/infrapilot; student research project)"


@dataclass(frozen=True)
class Paths:
    root: Path = PACKAGE_ROOT
    sources_dir: Path = PACKAGE_ROOT / "sources"
    data: Path = Path(os.environ.get("INFRAPILOT_DATA_DIR", PACKAGE_ROOT / "data"))

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def normalized(self) -> Path:
        return self.data / "normalized"

    @property
    def chunks(self) -> Path:
        return self.data / "chunks"

    @property
    def kb_export(self) -> Path:
        return self.data / "kb_export"

    @property
    def versions(self) -> Path:
        return self.data / "versions"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def http_cache(self) -> Path:
        return self.data / "cache" / "http"

    @property
    def vectors(self) -> Path:
        return self.data / "vectors"


@dataclass(frozen=True)
class ChunkingConfig:
    # Token counts use tiktoken cl100k_base as a proxy for the Titan tokenizer.
    target_tokens: int = 450
    max_tokens: int = 700
    min_tokens: int = 60
    overlap_tokens: int = 60


@dataclass(frozen=True)
class QualityConfig:
    min_doc_tokens: int = 40
    # A page whose lines are mostly short list items is a navigation/index page.
    nav_line_ratio: float = 0.7
    nav_max_tokens: int = 400
    # A line repeated in at least this share of a source's docs is boilerplate.
    boilerplate_doc_ratio: float = 0.5
    boilerplate_min_docs: int = 5
    near_dup_hamming: int = 3


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 1024
    # USD per 1M input tokens; check the Bedrock pricing page before relying on it.
    price_per_million_tokens: float = 0.02
    region: str = os.environ.get("AWS_REGION", "us-east-1")


@dataclass(frozen=True)
class Settings:
    paths: Paths = field(default_factory=Paths)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    # Minimum seconds between requests to the same host.
    host_delay: float = 1.0
    fast_hosts: tuple[str, ...] = ("api.github.com", "raw.githubusercontent.com")
    fast_host_delay: float = 0.2
    timeout: float = 30.0
    github_token: str | None = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


SETTINGS = Settings()
