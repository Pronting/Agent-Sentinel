from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


@async_retry(max_attempts=2)
async def get_metrics(alert_summary: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Fetching mock metrics summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        return {"latency_p95_ms": 1850, "error_rate": 0.087, "cpu_usage": 0.64}


@async_retry(max_attempts=2)
async def query_logs(alert_summary: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Querying mock logs summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        return {
            "matches": [
                "ERROR order-sync timeout after 3 retries",
                "WARN downstream payment-api slow response",
            ]
        }


@async_retry(max_attempts=2)
async def get_topology(alert_summary: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        logger.info("Fetching mock topology summary_chars=%s", len(alert_summary))
        await asyncio.sleep(0.05)
        return {"service": "order-sync", "dependencies": ["payment-api", "mysql-primary", "redis-cache"]}


async def fetch_all_live_data(alert_summary: str) -> dict[str, Any]:
    metrics, logs, topology = await asyncio.gather(
        get_metrics(alert_summary),
        query_logs(alert_summary),
        get_topology(alert_summary),
    )
    return {"metrics": metrics, "logs": logs, "topology": topology}
