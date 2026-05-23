from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.tools.base import LogsProvider, MetricsProvider, TopologyProvider

logger = logging.getLogger(__name__)


class CompositeToolsProvider:
    """组合工具提供者，支持部分降级

    可以将不同的 MetricsProvider、LogsProvider、TopologyProvider 组合在一起。
    未提供的子 provider 会在工厂函数中使用 Mock 实现。
    """

    def __init__(
        self,
        metrics_provider: MetricsProvider,
        logs_provider: LogsProvider,
        topology_provider: TopologyProvider,
    ) -> None:
        self._metrics = metrics_provider
        self._logs = logs_provider
        self._topology = topology_provider

    @property
    def metrics(self) -> MetricsProvider:
        return self._metrics

    @property
    def logs(self) -> LogsProvider:
        return self._logs

    @property
    def topology(self) -> TopologyProvider:
        return self._topology

    async def fetch_all(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """并发获取所有数据"""
        metrics, logs, topology = await asyncio.gather(
            self._metrics.get_metrics(alert_summary, group_id),
            self._logs.query_logs(alert_summary, group_id),
            self._topology.get_topology(alert_summary, group_id),
        )
        return {"metrics": metrics, "logs": logs, "topology": topology}
