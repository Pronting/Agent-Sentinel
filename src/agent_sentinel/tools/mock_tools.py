from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.monitoring import monitor
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


class MockMetricsProvider:
    """Mock 监控指标提供者"""

    @async_retry(max_attempts=2)
    async def get_metrics(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        async with asyncio.timeout(5):
            logger.info("Fetching mock metrics summary_chars=%s", len(alert_summary))
            await asyncio.sleep(0.05)
            monitor.record_tool_call("get_metrics", group_id)
            return {"latency_p95_ms": 1850, "error_rate": 0.087, "cpu_usage": 0.64}


class MockLogsProvider:
    """Mock 日志查询提供者"""

    @async_retry(max_attempts=2)
    async def query_logs(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
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


class MockTopologyProvider:
    """Mock 拓扑信息提供者"""

    @async_retry(max_attempts=2)
    async def get_topology(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        async with asyncio.timeout(5):
            logger.info("Fetching mock topology summary_chars=%s", len(alert_summary))
            await asyncio.sleep(0.05)
            monitor.record_tool_call("get_topology", group_id)
            return {"service": "order-sync", "dependencies": ["payment-api", "mysql-primary", "redis-cache"]}


class MockToolsProvider:
    """Mock 工具提供者，组合所有 Mock 子提供者"""

    def __init__(self) -> None:
        self._metrics = MockMetricsProvider()
        self._logs = MockLogsProvider()
        self._topology = MockTopologyProvider()

    @property
    def metrics(self) -> MockMetricsProvider:
        return self._metrics

    @property
    def logs(self) -> MockLogsProvider:
        return self._logs

    @property
    def topology(self) -> MockTopologyProvider:
        return self._topology

    async def fetch_all(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """并发获取所有数据"""
        metrics, logs, topology = await asyncio.gather(
            self._metrics.get_metrics(alert_summary, group_id),
            self._logs.query_logs(alert_summary, group_id),
            self._topology.get_topology(alert_summary, group_id),
        )
        return {"metrics": metrics, "logs": logs, "topology": topology}


# 向后兼容：保留原有函数接口
async def get_metrics(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    return await MockMetricsProvider().get_metrics(alert_summary, group_id)


async def query_logs(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    return await MockLogsProvider().query_logs(alert_summary, group_id)


async def get_topology(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    return await MockTopologyProvider().get_topology(alert_summary, group_id)


async def fetch_all_live_data(alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
    return await MockToolsProvider().fetch_all(alert_summary, group_id)
