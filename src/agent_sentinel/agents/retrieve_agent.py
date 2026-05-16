from __future__ import annotations

import logging

from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.rag.mock_retriever import MockRetriever

logger = logging.getLogger(__name__)


async def retrieve_node(state: DiagnosisState, retriever: MockRetriever) -> DiagnosisState:
    logger.info("Node retrieve started")
    docs = await retriever.retrieve(state.get("alert_summary", ""))
    logger.info("Node retrieve completed docs=%s", len(docs))
    return {
        "retrieved_docs": docs,
        "messages": append_message(state, "assistant", "历史案例检索完成，当前为 Mock 空实现。"),
        "evidence": append_evidence(state, f"RAG mock retriever returned {len(docs)} documents."),
    }


def should_fetch(state: DiagnosisState) -> str:
    text = f"{state.get('alert_summary', '')} {state.get('raw_alert', {})}".lower()
    keywords = ("error", "critical", "timeout", "latency", "失败", "超时", "异常", "错误")
    decision = "fetch" if any(keyword in text for keyword in keywords) else "skip"
    logger.info("Route should_fetch decision=%s", decision)
    return decision
