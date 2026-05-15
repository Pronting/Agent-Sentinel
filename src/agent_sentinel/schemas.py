from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User input for a single-turn conversation.")


class ChatResponse(BaseModel):
    answer: str
    model: str


class HealthResponse(BaseModel):
    status: str
    app: str


class AlertReportRequest(BaseModel):
    source: str = Field(..., min_length=1, description="Alert source, for example app, job, or service name.")
    level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"] = "ERROR"
    summary: str = Field(..., min_length=1, description="Short summary for the alert.")
    details: str | None = Field(default=None, description="Optional detailed context.")
    dedupe_key: str | None = Field(default=None, description="Optional dedupe key for short-window suppression.")
    tags: list[str] = Field(default_factory=list, description="Optional tags for grouping and filtering.")


class AlertReportResponse(BaseModel):
    status: str
    dispatched: bool
    deduplicated: bool


class AlertRecordResponse(BaseModel):
    source: str
    level: str
    summary: str
    details: str | None
    dedupe_key: str | None
    tags: list[str]
    created_at: str
