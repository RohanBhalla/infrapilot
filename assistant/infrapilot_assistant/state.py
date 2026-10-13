from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DialogState(BaseModel):
    """Per-session task state, saved with the session (DynamoDB in AWS)."""
    intent: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    asked: list[str] = Field(default_factory=list)       # slots asked about in the last turn
    awaiting_confirmation: bool = False
    confirmed: bool = False
    defaults_shown: bool = False
    status: Literal["idle", "collecting", "ready", "cancelled"] = "idle"


class Question(BaseModel):
    slot: str
    question: str
    options: list[str] = Field(default_factory=list)


class Turn(BaseModel):
    """What the assistant decided for one message."""
    type: Literal["answer", "clarification", "confirmation", "ready", "out_of_scope"]
    intent: str
    confidence: float
    standalone_question: str
    message: str = ""                                    # reply text (not used for "answer")
    params: dict[str, Any] = Field(default_factory=dict) # task details, incl. defaults when ready
    questions: list[Question] = Field(default_factory=list)
    error_text: str | None = None
    state: DialogState
    cost_usd: float = 0.0
