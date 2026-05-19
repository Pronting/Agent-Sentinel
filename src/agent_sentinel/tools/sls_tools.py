from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent_sentinel.config import Settings, get_settings
from agent_sentinel.utils.retry_utils import async_retry

logger = logging.getLogger(__name__)

# 告警摘要中常见的关键词，用于构造 SLS 查询条件
_ERROR_KEYWORDS = re.compile(
    r"(error|exception|timeout|fatal|panic|fail|crash|oom|5[0-9]{2})",
    re.IGNORECASE,
)

# 线程本地存储，保证每个线程有独立的 LogClient 实例
_thread_local = threading.local()


def _build_sls_query(alert_summary: str) -> str:
    """从告警摘要中提取关键词，构造 SLS 全文检索查询表达式。"""
    keywords = _ERROR_KEYWORDS.findall(alert_summary)
    if not keywords:
        # 用双引号包裹原文做短语搜索，避免特殊字符破坏查询语法
        safe_text = alert_summary[:100].replace('"', '\\"')
        return f'"{safe_text}"'
    # SLS 全文检索用 and 连接关键词，去重后取前 5 个
    seen: set[str] = set()
    unique: list[str] = []
    for k in keywords:
        lk = k.lower()
        if lk not in seen:
            seen.add(lk)
            unique.append(lk)
        if len(unique) >= 5:
            break
    return " and ".join(unique)


def _sls_config_ready(settings: Settings) -> bool:
    """检查 SLS 必要参数是否全部配置。"""
    return all([
        settings.sls_endpoint,
        settings.sls_access_key_id,
        settings.sls_access_key_secret,
        settings.sls_project,
        settings.sls_logstore,
    ])


def _get_client(settings: Settings):
    """获取线程本地的 SLS LogClient 实例，避免重复创建连接。"""
    from aliyun.log import LogClient

    if not hasattr(_thread_local, "client"):
        _thread_local.client = LogClient(
            settings.sls_endpoint,
            settings.sls_access_key_id,
            settings.sls_access_key_secret,
        )
    return _thread_local.client


def _query_logs_sync(settings: Settings, query: str, time_range_seconds: int) -> list[str]:
    """同步调用 SLS GetLogs API，返回日志内容列表。"""
    client = _get_client(settings)
    from_time = int(time.time()) - time_range_seconds
    to_time = int(time.time())

    response = client.get_log(
        project=settings.sls_project,
        logstore=settings.sls_logstore,
        from_time=from_time,
        to_time=to_time,
        topic="",
        query=query,
        size=20,
        offset=0,
        reverse=False,
    )

    matches: list[str] = []
    for log_item in response.get_logs():
        # 将每条日志的所有字段拼接为一行文本
        parts = [f"{k}={v}" for k, v in log_item.get_contents().items()]
        matches.append(" | ".join(parts))
    return matches


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True,
)
async def query_logs(alert_summary: str) -> dict[str, Any]:
    """查询阿里云 SLS 日志。配置不完整时降级为 mock。"""
    settings = get_settings()
    if not _sls_config_ready(settings):
        logger.warning("SLS config incomplete, falling back to mock logs")
        from agent_sentinel.tools.mock_tools import query_logs as mock_query_logs

        return await mock_query_logs(alert_summary)

    query = _build_sls_query(alert_summary)
    time_range = settings.sls_query_time_range_seconds
    logger.info(
        "Querying SLS project=%s logstore=%s query=%r time_range=%ds",
        settings.sls_project,
        settings.sls_logstore,
        query,
        time_range,
    )

    async with asyncio.timeout(10):
        loop = asyncio.get_running_loop()
        matches = await loop.run_in_executor(
            None,
            _query_logs_sync,
            settings,
            query,
            time_range,
        )

    logger.info("SLS returned %d log entries", len(matches))
    return {"matches": matches}


@async_retry(max_attempts=2)
async def get_metrics(alert_summary: str) -> dict[str, Any]:
    """获取指标数据。当前仍使用 mock，后续可对接 ARMS/Prometheus。"""
    from agent_sentinel.tools.mock_tools import get_metrics as mock_get_metrics

    return await mock_get_metrics(alert_summary)


@async_retry(max_attempts=2)
async def get_topology(alert_summary: str) -> dict[str, Any]:
    """获取拓扑数据。当前仍使用 mock，后续可对接 K8s/服务网格。"""
    from agent_sentinel.tools.mock_tools import get_topology as mock_get_topology

    return await mock_get_topology(alert_summary)


async def fetch_all_live_data(alert_summary: str) -> dict[str, Any]:
    """并发获取指标、日志、拓扑数据。"""
    metrics, logs, topology = await asyncio.gather(
        get_metrics(alert_summary),
        query_logs(alert_summary),
        get_topology(alert_summary),
    )
    return {"metrics": metrics, "logs": logs, "topology": topology}
