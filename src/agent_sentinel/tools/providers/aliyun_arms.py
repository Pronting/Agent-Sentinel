from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.monitoring import monitor
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


class AliyunARMSClient:
    """阿里云 ARMS 客户端

    封装 alibabacloud-arms20190808 SDK，提供异步接口。
    用于查询应用监控指标（QPS、延迟、错误率等）。
    """

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region_id: str = "cn-hangzhou",
        app_id: str = "",
        timeout_seconds: int = 10,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region_id = region_id
        self._app_id = app_id
        self._timeout_seconds = timeout_seconds
        self._client = None

    def _get_client(self):
        """延迟初始化 ARMS 客户端"""
        if self._client is None:
            try:
                from alibabacloud_arms20190808.client import Client as ARMSClient
                from alibabacloud_tea_openapi import models as open_api_models

                config = open_api_models.Config(
                    access_key_id=self._access_key_id,
                    access_key_secret=self._access_key_secret,
                    region_id=self._region_id,
                )
                self._client = ARMSClient(config)
            except ImportError:
                raise ImportError(
                    "alibabacloud-arms20190808 is required for ARMS provider. "
                    "Install it with: pip install alibabacloud-arms20190808"
                )
        return self._client

    @async_retry(max_attempts=2)
    async def get_app_metrics(
        self,
        app_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        """获取应用监控指标

        Args:
            app_id: ARMS 应用 ID，默认使用配置的 app_id
            start_time: 开始时间戳（毫秒），默认为 1 小时前
            end_time: 结束时间戳（毫秒），默认为当前时间

        Returns:
            包含 QPS、延迟、错误率等指标的字典
        """
        import time

        client = self._get_client()
        app_id = app_id or self._app_id

        if not app_id:
            logger.warning("ARMS app_id not configured, returning empty metrics")
            return {}

        # 默认查询最近 1 小时
        now = int(time.time() * 1000)
        start_time = start_time or (now - 3600 * 1000)
        end_time = end_time or now

        try:
            async with asyncio.timeout(self._timeout_seconds):
                loop = asyncio.get_event_loop()
                metrics = await loop.run_in_executor(
                    None,
                    lambda: self._query_app_metrics(client, app_id, start_time, end_time),
                )
                return metrics

        except Exception as e:
            logger.error("ARMS metrics query failed: %s", e)
            raise

    def _query_app_metrics(
        self,
        client,
        app_id: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, Any]:
        """查询应用指标的具体实现"""
        try:
            from alibabacloud_arms20190808 import models as arms_models

            # 查询应用概览指标
            request = arms_models.QueryAppOverviewRequest(
                app_id=app_id,
                start_time=start_time,
                end_time=end_time,
            )
            response = client.query_app_overview(request)
            result = response.to_map().get("body", {})

            return {
                "qps": result.get("qps", 0),
                "latency_p95_ms": result.get("rt_p95", 0),
                "error_rate": result.get("error_rate", 0),
                "cpu_usage": result.get("cpu_usage", 0),
                "memory_usage": result.get("memory_usage", 0),
            }
        except Exception as e:
            logger.warning("Failed to query app metrics: %s, returning empty", e)
            return {}


class ARMSMetricsProvider:
    """基于 ARMS 的监控指标提供者"""

    def __init__(self, client: AliyunARMSClient) -> None:
        self._client = client

    async def get_metrics(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """获取监控指标

        Args:
            alert_summary: 告警摘要文本
            group_id: 飞书群组 ID（可用于映射到不同的 ARMS 应用）

        Returns:
            包含监控指标的字典
        """
        logger.info("Fetching ARMS metrics summary_chars=%s group_id=%s", len(alert_summary), group_id)

        # 查询指标（重试已在 AliyunARMSClient.get_app_metrics 中处理）
        metrics = await self._client.get_app_metrics()

        monitor.record_tool_call("get_metrics", group_id)

        return {
            **metrics,
            "provider": "aliyun_arms",
        }
