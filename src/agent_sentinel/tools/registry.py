from __future__ import annotations

import logging
from typing import Any

from agent_sentinel.config import Settings

logger = logging.getLogger(__name__)


async def fetch_all_live_data(alert_summary: str, settings: Settings) -> dict[str, Any]:
    """根据 tools_provider 配置分发到真实工具或 mock 工具。

    - tools_provider="real"  → 使用 sls_tools（内部会检查 SLS 配置，不完整时自动降级 mock）
    - tools_provider="mock" 或其他 → 使用 mock_tools
    """
    provider = settings.tools_provider

    if provider == "real":
        logger.info("Using real tools (SLS) for live data fetch")
        from agent_sentinel.tools.sls_tools import fetch_all_live_data as real_fetch

        return await real_fetch(alert_summary)

    logger.info("Using mock tools for live data fetch")
    from agent_sentinel.tools.mock_tools import fetch_all_live_data as mock_fetch

    return await mock_fetch(alert_summary)
