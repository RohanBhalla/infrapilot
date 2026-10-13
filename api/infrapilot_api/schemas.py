"""Request/response shapes. FastAPI publishes them in the OpenAPI spec at /openapi.json."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from infrapilot_assistant.state import DialogState, Question
from infrapilot_rag.models import Citation


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Filters(BaseModel):
    """Optional metadata filters; each field takes one value or a list."""
    model_config = ConfigDict(extra="forbid")
    category: str | list[str] | None = None
    doc_type: str | list[str] | None = None
    source_id: str | list[str] | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=2000, description="The user's message")
    filters: Filters | None = None


class Message(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(default_factory=utcnow)
    citations: list[Citation] = Field(default_factory=list)
    refused: bool | None = None


class MessageOut(BaseModel):
    session_id: str
    type: Literal["answer", "clarification", "confirmation", "ready", "out_of_scope"] = Field(
        description="answer: from the docs, with citations. clarification: questions for missing details. "
                    "confirmation: check the task before starting. ready: all details known. "
                    "out_of_scope: not something InfraPilot does.")
    message: Message = Field(description="The assistant's reply")
    intent: str
    confidence: float
    params: dict = Field(default_factory=dict, description="Task details so far (with defaults when ready)")
    questions: list[Question] = Field(default_factory=list, description="Clarification questions, one per detail")
    standalone_question: str = Field(description="The message rewritten to stand alone (what gets searched)")
    model: str | None
    latency_ms: int
    cost_usd: float


class Session(BaseModel):
    id: str
    created_at: datetime = Field(default_factory=utcnow)
    messages: list[Message] = Field(default_factory=list)
    dialog: DialogState = Field(default_factory=DialogState, description="Task in progress, if any")


class SessionCreated(BaseModel):
    id: str
    created_at: datetime
