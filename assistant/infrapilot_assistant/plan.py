"""`Plan`: what a generator returns (plan contract 5). The chat shows it; later the request
workflow (C12) stores it and deploys it after approval."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlanFile(BaseModel):
    path: str                       # where the file goes in the user's repo
    content: str
    language: str = "yaml"          # for syntax highlighting


class Validation(BaseModel):
    tool: str                       # e.g. actionlint, version-pins
    status: Literal["pass", "fail", "skipped"]
    findings: list[str] = Field(default_factory=list)


class Source(BaseModel):
    title: str
    url: str


class Plan(BaseModel):
    intent: str
    params: dict[str, Any]
    files: list[PlanFile]
    summary: str                    # what the files do, in a few lines
    setup: list[str] = Field(default_factory=list)    # what the user has to do once (secrets, roles)
    notes: list[str] = Field(default_factory=list)    # assumptions and things we changed
    validations: list[Validation] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)
    estimated_cost_note: str | None = None

    @property
    def valid(self) -> bool:
        return all(v.status != "fail" for v in self.validations)
