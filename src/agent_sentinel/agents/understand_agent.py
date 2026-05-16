from __future__ import annotations

import asyncio
import logging

from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.llm.executor import LLMExecutor
from agent_sentinel.utils.config_loader import format_prompt
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


@async_retry(max_attempts=2)
async def understand_node(state: DiagnosisState, llm: LLMExecutor) -> DiagnosisState:
    logger.info("Node understand started")
    prompt = format_prompt("understand", {"raw_alert": state.get("raw_alert", {})})
    async with asyncio.timeout(10):
        summary = await llm.call(prompt)
    logger.info("Node understand completed summary_chars=%s", len(summary))
    return {
        "alert_summary": summary,
        "messages": append_message(state, "assistant", f"告警理解完成: {summary}"),
        "evidence": append_evidence(state, "LLM generated initial alert summary."),
    }
