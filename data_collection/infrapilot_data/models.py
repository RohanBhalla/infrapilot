"""Data contracts for each pipeline stage.

raw (RawDoc) -> normalized (NormalizedDoc) -> chunks (Chunk) -> KB export / vectors.
VersionFact records go to the version registry (local JSON now, DynamoDB later).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

DocType = Literal[
    "reference", "guide", "release_note", "rule", "troubleshooting", "template", "version"
]
ContentFormat = Literal["markdown", "html", "text", "code"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class SourceSpec(BaseModel):
    id: str
    category: str
    name: str
    url: str
    sheet_type: str
    notes: str = ""
    origin: Literal["sheet", "additional"] = "sheet"
    strategy: str
    doc_type: DocType = "reference"
    params: dict[str, Any] = Field(default_factory=dict)
    skip_reason: str | None = None

    @property
    def enabled(self) -> bool:
        return self.strategy != "skip"


class RawDoc(BaseModel):
    source_id: str
    url: str
    title: str | None = None
    format: ContentFormat
    body: str
    fetched_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = None
    language: str | None = None  # for code docs, e.g. "yaml"
    extra: dict[str, Any] = Field(default_factory=dict)


class NormalizedDoc(BaseModel):
    doc_id: str
    source_id: str
    category: str
    doc_type: DocType
    title: str
    url: str
    fetched_at: datetime
    published_at: datetime | None = None
    content_hash: str
    token_count: int
    body_markdown: str


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    source_id: str
    category: str
    doc_type: DocType
    title: str
    url: str
    heading_path: list[str]
    chunk_index: int
    token_count: int
    content_hash: str
    fetched_at: datetime
    # What gets embedded: a short context header followed by the chunk body.
    text: str


class VersionFact(BaseModel):
    tool: str
    latest: str | None = None
    lts: str | None = None
    eol: str | None = None
    released_at: str | None = None
    source_url: str
    fetched_at: datetime = Field(default_factory=utcnow)
    extra: dict[str, Any] = Field(default_factory=dict)


class FetchResult(BaseModel):
    source_id: str
    docs: list[RawDoc] = Field(default_factory=list)
    versions: list[VersionFact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped_urls: list[str] = Field(default_factory=list)
