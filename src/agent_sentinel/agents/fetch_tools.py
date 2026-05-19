from __future__ import annotations

import logging

from agent_sentinel.config import get_settings
from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message
from agent_sentinel.tools.registry import fetch_all_live_data

logger = logging.getLogger(__name__)


async def fetch_live_data_node(state: DiagnosisState) -> DiagnosisState:
    logger.info("Node fetch_live_data started")
    settings = get_settings()
    live_data = await fetch_all_live_data(state.get("alert_summary", ""), settings)
    provider = "SLS" if settings.tools_provider == "real" else "Mock"
    logger.info("Node fetch_live_data completed provider=%s keys=%s", provider, sorted(live_data.keys()))
    return {
        "live_data": live_data,
        "messages": append_message(state, "assistant", f"实时指标、日志、拓扑数据已并发获取（数据源：{provider}）。"),
        "evidence": append_evidence(state, f"Live metrics/logs/topology fetched via {provider} provider."),
    }
