from __future__ import annotations

from typing import Any, TypedDict


class DiagnosisState(TypedDict, total=False):
    raw_alert: dict[str, Any]
    alert_summary: str
    retrieved_docs: list[str]
    live_data: dict[str, Any]
    recommended_plan: dict[str, Any]
    evidence: list[str]
    validation_result: bool
    need_human: bool
    messages: list[dict[str, str]]
    chat_id: str
    thread_root_message_id: str | None
    mention_open_id: str | None
    mention_name: str | None
    decision_id: str
    human_decision: str
    validation_attempts: int
    final_text: str


def append_message(state: DiagnosisState, role: str, content: str) -> list[dict[str, str]]:
    return [*state.get("messages", []), {"role": role, "content": content}]


def append_evidence(state: DiagnosisState, *items: str) -> list[str]:
    return [*state.get("evidence", []), *[item for item in items if item]]
