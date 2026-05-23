from __future__ import annotations

from typing import Any, Protocol


class MetricsProvider(Protocol):
    """监控指标提供者接口"""

    async def get_metrics(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """获取监控指标"""
        ...


class LogsProvider(Protocol):
    """日志查询提供者接口"""

    async def query_logs(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """查询日志"""
        ...


class TopologyProvider(Protocol):
    """拓扑信息提供者接口"""

    async def get_topology(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """获取服务拓扑"""
        ...


class ToolsProvider(Protocol):
    """完整的工具提供者接口"""

    @property
    def metrics(self) -> MetricsProvider:
        ...

    @property
    def logs(self) -> LogsProvider:
        ...

    @property
    def topology(self) -> TopologyProvider:
        ...

    async def fetch_all(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """并发获取所有数据"""
        ...
