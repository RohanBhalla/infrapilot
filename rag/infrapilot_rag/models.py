from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Metadata filters: field -> one value or a list of allowed values.
Filters = dict[str, str | list[str]]
FILTER_FIELDS = ("category", "doc_type", "source_id")


class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    url: str
    title: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    n: int
    title: str
    url: str
    chunk_id: str


class Answer(BaseModel):
    question: str
    text: str
    refused: bool
    citations: list[Citation] = Field(default_factory=list)
    invalid_citations: list[int] = Field(default_factory=list)
    cited_versions: bool = False
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    versions_used: list[str] = Field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
