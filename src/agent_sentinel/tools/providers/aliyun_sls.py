from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent_sentinel.monitoring import monitor
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)


class AliyunSLSClient:
    """阿里云 SLS 日志服务客户端

    封装 aliyun-log-python-sdk，提供异步接口。
    SDK 是同步的，通过 run_in_executor 异步化。
    """

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str,
        project: str,
        logstore: str,
        timeout_seconds: int = 10,
        max_lines: int = 100,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._endpoint = endpoint
        self._project = project
        self._logstore = logstore
        self._timeout_seconds = timeout_seconds
        self._max_lines = max_lines
        self._client = None

    def _get_client(self):
        """延迟初始化 SLS 客户端"""
        if self._client is None:
            try:
                from aliyun.log.logclient import LogClient

                self._client = LogClient(
                    self._endpoint,
                    self._access_key_id,
                    self._access_key_secret,
                )
            except ImportError:
                raise ImportError(
                    "aliyun-log-python-sdk is required for SLS provider. "
                    "Install it with: pip install aliyun-log-python-sdk"
                )
        return self._client

    @async_retry(max_attempts=2)
    async def query_logs(
        self,
        query: str,
        from_time: int | None = None,
        to_time: int | None = None,
        max_lines: int | None = None,
    ) -> list[dict[str, Any]]:
        """查询 SLS 日志

        Args:
            query: SLS 查询语句（LOGQL 语法）
            from_time: 开始时间戳（秒），默认为 1 小时前
            to_time: 结束时间戳（秒），默认为当前时间
            max_lines: 最大返回行数

        Returns:
            日志列表，每条包含 timestamp 和 content
        """
        from aliyun.log.logclient import LogException

        client = self._get_client()
        max_lines = max_lines or self._max_lines

        # 默认查询最近 1 小时
        now = int(time.time())
        from_time = from_time or (now - 3600)
        to_time = to_time or now

        try:
            async with asyncio.timeout(self._timeout_seconds):
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: client.get_log_lines(
                        self._project,
                        self._logstore,
                        topic="",
                        from_time=from_time,
                        to_time=to_time,
                        query=query,
                        max_line_num=max_lines,
                    ),
                )

                logs = []
                for log in result.get_log_lines():
                    logs.append({
                        "timestamp": log.get_time(),
                        "content": log.get_contents(),
                    })
                return logs

        except LogException as e:
            logger.error("SLS query failed: code=%s message=%s", e.get_error_code(), e.get_error_message())
            raise
        except Exception as e:
            logger.error("SLS query unexpected error: %s", e)
            raise


class SLSLogsProvider:
    """基于 SLS 的日志查询提供者"""

    def __init__(self, client: AliyunSLSClient) -> None:
        self._client = client

    async def query_logs(self, alert_summary: str, group_id: str | None = None) -> dict[str, Any]:
        """查询与告警相关的日志

        Args:
            alert_summary: 告警摘要文本
            group_id: 飞书群组 ID（可用于映射到不同的 project/logstore）

        Returns:
            包含 matches 列表的字典
        """
        logger.info("Querying SLS logs summary_chars=%s group_id=%s", len(alert_summary), group_id)

        # 构建查询语句
        query = self._build_query(alert_summary)

        # 查询日志（重试已在 AliyunSLSClient.query_logs 中处理）
        logs = await self._client.query_logs(query=query)

        monitor.record_tool_call("query_logs", group_id)

        return {
            "matches": [
                log.get("content", {}).get("message", log.get("content", {}).get("__content__", str(log)))
                for log in logs
            ],
            "total": len(logs),
            "provider": "aliyun_sls",
        }

    def _build_query(self, alert_summary: str) -> str:
        """根据告警摘要构建 SLS 查询语句

        从告警摘要中提取关键词，构建 OR 查询。
        """
        # 提取关键词（简单实现：按空格分割，取前 5 个长度 > 2 的词）
        keywords = [kw for kw in alert_summary.split() if len(kw) > 2][:5]

        if not keywords:
            return "* AND ERROR"

        # 构建 SLS 查询：关键词用 OR 连接
        query_parts = [f'"{kw}"' for kw in keywords]
        return " OR ".join(query_parts)
