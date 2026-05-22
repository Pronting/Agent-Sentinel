from __future__ import annotations

import logging

from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.monitoring import monitor
from agent_sentinel.rag.base import BaseRetriever
from agent_sentinel.rag.models import RagFilters

logger = logging.getLogger(__name__)


async def retrieve_node(state: DiagnosisState, retriever: BaseRetriever) -> DiagnosisState:
    logger.info("Node retrieve started")
    monitor.record_rag_retrieval("hybrid", state.get("chat_id"))
    filters = _build_filters(state)
    docs = await retriever.retrieve(state.get("alert_summary", ""), filters)
    prompt_docs = [doc.to_prompt_text() for doc in docs]
    logger.info("Node retrieve completed docs=%s", len(prompt_docs))
    return {
        "retrieved_docs": prompt_docs,
        "messages": append_message(state, "assistant", f"RAG 检索完成，召回 {len(prompt_docs)} 条上下文。"),
        "evidence": append_evidence(state, f"Hybrid RAG returned {len(prompt_docs)} documents."),
    }


def should_fetch(state: DiagnosisState) -> str:
    text = f"{state.get('alert_summary', '')} {state.get('raw_alert', {})}".lower()
    keywords = ("error", "critical", "timeout", "latency", "失败", "超时", "异常", "错误")
    decision = "fetch" if any(keyword in text for keyword in keywords) else "skip"
    logger.info("Route should_fetch decision=%s", decision)
    return decision


def _build_filters(state: DiagnosisState) -> RagFilters:
    raw_alert = state.get("raw_alert", {})
    return RagFilters(
        service=_clean(raw_alert.get("service") or raw_alert.get("source")),
        level=_clean(raw_alert.get("level")),
        chat_id=_clean(state.get("chat_id")),
        tags=[str(tag) for tag in raw_alert.get("tags", [])],
    )


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
