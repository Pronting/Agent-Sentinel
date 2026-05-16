from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SourceType = Literal["static_doc", "message_history", "mock"]


class RetrievedDoc(BaseModel):
    id: str
    text: str
    source_type: SourceType
    score: float = 0.0
    weighted_score: float = 0.0
    service: str | None = None
    title: str | None = None
    created_at: int | None = None
    source_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list, exclude=True)

    def to_prompt_text(self) -> str:
        label = "固定知识" if self.source_type == "static_doc" else "历史消息"
        title = self.title or self.id
        service = f" service={self.service}" if self.service else ""
        uri = f" source={self.source_uri}" if self.source_uri else ""
        return f"[{label}] {title}{service}{uri}\n{self.text}"


class RagFilters(BaseModel):
    service: str | None = None
    level: str | None = None
    chat_id: str | None = None
    tags: list[str] = Field(default_factory=list)
