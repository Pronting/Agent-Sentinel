from __future__ import annotations

import logging

from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.monitoring import monitor
from agent_sentinel.tools.mock_tools import fetch_all_live_data

logger = logging.getLogger(__name__)


async def fetch_live_data_node(state: DiagnosisState) -> DiagnosisState:
    logger.info("Node fetch_live_data started")
    group_id = state.get("chat_id")
    try:
        live_data = await fetch_all_live_data(state.get("alert_summary", ""), group_id=group_id)
    except Exception:
        monitor.record_error("tool_call_error")
        raise
    logger.info("Node fetch_live_data completed keys=%s", sorted(live_data.keys()))
    return {
        "live_data": live_data,
        "messages": append_message(state, "assistant", "实时指标、日志、拓扑 Mock 数据已并发获取。"),
        "evidence": append_evidence(state, "Live metrics/logs/topology mock tools completed concurrently."),
    }
