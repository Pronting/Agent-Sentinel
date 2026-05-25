from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from agent_sentinel.monitoring import monitor
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)

# 日志级别关键词
LOG_LEVELS = ["ERROR", "WARN", "WARNING", "FATAL", "CRITICAL", "EXCEPTION", "EXCEPTION"]

# 常见服务名模式
SERVICE_PATTERNS = [
    r"[\w-]+-service",
    r"[\w-]+-api",
    r"[\w-]+-sync",
    r"[\w-]+-worker",
    r"[\w-]+-consumer",
]


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
                    # 解析日志内容
                    contents = log.get_contents()
                    log_entry = {
                        "timestamp": log.get_time(),
                        "content": contents,
                    }

                    # 提取常见字段
                    if isinstance(contents, dict):
                        log_entry["level"] = contents.get("level", contents.get("severity", ""))
                        log_entry["service"] = contents.get("service", contents.get("app", ""))
                        log_entry["message"] = contents.get("message", contents.get("msg", ""))
                        log_entry["trace_id"] = contents.get("trace_id", contents.get("traceId", ""))
                        log_entry["span_id"] = contents.get("span_id", contents.get("spanId", ""))
                    else:
                        log_entry["message"] = str(contents)

                    logs.append(log_entry)
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
            包含 matches 列表和统计信息的字典
        """
        logger.info("Querying SLS logs summary_chars=%s group_id=%s", len(alert_summary), group_id)

        # 构建查询语句
        query = self._build_query(alert_summary)
        logger.info("SLS query: %s", query)

        # 查询日志（重试已在 AliyunSLSClient.query_logs 中处理）
        logs = await self._client.query_logs(query=query)

        monitor.record_tool_call("query_logs", group_id)

        # 统计日志级别分布
        level_stats = {}
        for log in logs:
            level = log.get("level", "UNKNOWN")
            level_stats[level] = level_stats.get(level, 0) + 1

        # 提取错误消息
        error_messages = []
        for log in logs:
            level = log.get("level", "").upper()
            if level in ["ERROR", "FATAL", "CRITICAL", "EXCEPTION"]:
                error_messages.append(log.get("message", str(log.get("content", ""))))

        return {
            "matches": [
                {
                    "timestamp": log.get("timestamp"),
                    "level": log.get("level", ""),
                    "service": log.get("service", ""),
                    "message": log.get("message", str(log.get("content", ""))),
                    "trace_id": log.get("trace_id", ""),
                }
                for log in logs
            ],
            "total": len(logs),
            "level_stats": level_stats,
            "error_messages": error_messages[:5],  # 最多返回 5 条错误消息
            "query": query,
            "provider": "aliyun_sls",
        }

    def _build_query(self, alert_summary: str) -> str:
        """根据告警摘要构建 SLS 查询语句

        智能分析告警摘要，提取日志级别、服务名和关键词，构建高效的查询语句。

        Args:
            alert_summary: 告警摘要文本

        Returns:
            SLS 查询语句（LOGQL 语法）
        """
        # 提取日志级别
        log_level = self._extract_log_level(alert_summary)

        # 提取服务名
        service_name = self._extract_service_name(alert_summary)

        # 提取关键词
        keywords = self._extract_keywords(alert_summary)

        # 构建查询条件
        conditions = []

        # 添加日志级别条件
        if log_level:
            conditions.append(f'level: {log_level}')

        # 添加服务名条件
        if service_name:
            conditions.append(f'service: "{service_name}"')

        # 添加关键词条件
        if keywords:
            keyword_query = " OR ".join([f'"{kw}"' for kw in keywords[:3]])
            conditions.append(f'({keyword_query})')

        # 组合查询条件
        if not conditions:
            return '* AND (level: ERROR OR level: WARN)'

        if len(conditions) == 1:
            return conditions[0]

        return " AND ".join(conditions)

    def _extract_log_level(self, text: str) -> str | None:
        """从文本中提取日志级别"""
        text_upper = text.upper()
        for level in LOG_LEVELS:
            if level in text_upper:
                return level
        return None

    def _extract_service_name(self, text: str) -> str | None:
        """从文本中提取服务名"""
        for pattern in SERVICE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词

        过滤停用词，提取有意义的关键词。
        """
        # 停用词列表
        stop_words = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
            "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
            "自己", "这", "他", "她", "它", "们", "那", "被", "从", "把", "让", "用", "对",
            "等", "但", "而", "如果", "虽然", "因为", "所以", "这个", "那个", "什么", "怎么",
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "can", "shall", "to", "of", "in", "for",
            "on", "with", "at", "by", "from", "as", "into", "through", "during",
            "before", "after", "above", "below", "between", "out", "off", "over",
            "under", "again", "further", "then", "once", "and", "but", "or",
            "nor", "not", "so", "very", "just", "than", "too", "also",
        }

        # 提取单词（支持中英文）
        words = re.findall(r'[\w\u4e00-\u9fff]+', text)

        # 过滤停用词和短词
        keywords = [
            word for word in words
            if len(word) > 2 and word.lower() not in stop_words
        ]

        # 去重并限制数量
        seen = set()
        result = []
        for kw in keywords:
            if kw.lower() not in seen:
                seen.add(kw.lower())
                result.append(kw)

        return result[:5]
