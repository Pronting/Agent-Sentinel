from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field

from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.llm.executor import LLMExecutor
from agent_sentinel.utils.config_loader import format_prompt
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


class PlanOutput(BaseModel):
    recommended_plan: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    need_human: bool = True


@async_retry(max_attempts=2)
async def generate_plan_node(state: DiagnosisState, llm: LLMExecutor) -> DiagnosisState:
    logger.info("Node generate_plan started")
    parser = PydanticOutputParser(pydantic_object=PlanOutput)
    prompt = format_prompt(
        "generate_plan",
        {
            "alert_summary": state.get("alert_summary", ""),
            "retrieved_docs": state.get("retrieved_docs", []),
            "live_data": state.get("live_data", {}),
            "evidence": state.get("evidence", []),
        },
    )
    prompt = f"{prompt}\n\n{parser.get_format_instructions()}"
    async with asyncio.timeout(10):
        raw = await llm.call(prompt)
    parsed = parser.parse(raw)
    logger.info("Node generate_plan completed evidence=%s", len(parsed.evidence))
    return {
        "recommended_plan": parsed.recommended_plan,
        "evidence": append_evidence(state, *parsed.evidence),
        "need_human": parsed.need_human,
        "messages": append_message(state, "assistant", "诊断方案生成完成，已附带证据链。"),
    }
