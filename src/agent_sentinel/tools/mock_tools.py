from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.monitoring import monitor
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


@async_retry(max_attempts=2)
async def get_metrics(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Fetching mock metrics summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        monitor.record_tool_call("get_metrics", group_id)
        return {"latency_p95_ms": 1850, "error_rate": 0.087, "cpu_usage": 0.64}


@async_retry(max_attempts=2)
async def query_logs(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Querying mock logs summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        monitor.record_tool_call("query_logs", group_id)
        return {
            "matches": [
                "ERROR order-sync timeout after 3 retries",
                "WARN downstream payment-api slow response",
            ]
        }


@async_retry(max_attempts=2)
async def get_topology(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Fetching mock topology summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        monitor.record_tool_call("get_topology", group_id)
        return {"service": "order-sync", "dependencies": ["payment-api", "mysql-primary", "redis-cache"]}


async def fetch_all_live_data(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    metrics, logs, topology = await asyncio.gather(
        get_metrics(alert_summary, group_id),
        query_logs(alert_summary, group_id),
        get_topology(alert_summary, group_id),
    )
    return {"metrics": metrics, "logs": logs, "topology": topology}
