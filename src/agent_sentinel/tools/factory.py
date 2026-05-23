from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_sentinel.config import Settings
    from agent_sentinel.tools.base import ToolsProvider

logger = logging.getLogger(__name__)


def build_tools_provider(settings: Settings) -> ToolsProvider:
    """构建工具提供者

    Args:
        settings: Settings 配置对象

    Returns:
        ToolsProvider 实现（MockToolsProvider 或 CompositeToolsProvider）
    """
    from agent_sentinel.tools.mock_tools import MockToolsProvider

    provider = settings.tools_provider.strip().lower()

    if provider != "aliyun":
        logger.info("Using mock tools provider provider=%s", provider)
        return MockToolsProvider()

    # 检查阿里云配置是否完整
    if not _validate_aliyun_config(settings):
        logger.warning("Aliyun config incomplete, falling back to mock tools provider")
        return MockToolsProvider()

    try:
        return _build_aliyun_provider(settings)
    except ImportError as e:
        logger.warning("Aliyun SDK not available: %s, falling back to mock", e)
        return MockToolsProvider()
    except Exception as e:
        logger.error("Failed to initialize aliyun provider: %s, falling back to mock", e)
        return MockToolsProvider()


def _validate_aliyun_config(settings: Settings) -> bool:
    """验证阿里云配置是否完整"""
    # 检查 SLS 配置
    sls_configured = bool(
        settings.aliyun_sls_access_key_id
        and settings.aliyun_sls_access_key_secret
        and settings.aliyun_sls_project
        and settings.aliyun_sls_logstore
    )

    # 检查 ARMS 配置
    arms_configured = bool(
        settings.aliyun_arms_access_key_id
        and settings.aliyun_arms_access_key_secret
        and settings.aliyun_arms_app_id
    )

    # 至少需要配置一个
    return sls_configured or arms_configured


def _build_aliyun_provider(settings: Settings) -> ToolsProvider:
    """构建阿里云工具提供者"""
    from agent_sentinel.tools.mock_tools import (
        MockLogsProvider,
        MockMetricsProvider,
        MockTopologyProvider,
    )

    # 构建 SLS 客户端
    logs_provider = None
    if settings.aliyun_sls_access_key_id and settings.aliyun_sls_access_key_secret:
        from agent_sentinel.tools.providers.aliyun_sls import AliyunSLSClient, SLSLogsProvider

        sls_client = AliyunSLSClient(
            access_key_id=settings.aliyun_sls_access_key_id,
            access_key_secret=settings.aliyun_sls_access_key_secret,
            endpoint=settings.aliyun_sls_endpoint,
            project=settings.aliyun_sls_project,
            logstore=settings.aliyun_sls_logstore,
            timeout_seconds=settings.aliyun_sls_query_timeout_seconds,
            max_lines=settings.aliyun_sls_max_lines,
        )
        logs_provider = SLSLogsProvider(sls_client)

    # 构建 ARMS 客户端
    metrics_provider = None
    if settings.aliyun_arms_access_key_id and settings.aliyun_arms_access_key_secret:
        from agent_sentinel.tools.providers.aliyun_arms import AliyunARMSClient, ARMSMetricsProvider

        arms_client = AliyunARMSClient(
            access_key_id=settings.aliyun_arms_access_key_id,
            access_key_secret=settings.aliyun_arms_access_key_secret,
            region_id=settings.aliyun_arms_region_id,
            app_id=settings.aliyun_arms_app_id,
            timeout_seconds=settings.aliyun_arms_query_timeout_seconds,
        )
        metrics_provider = ARMSMetricsProvider(arms_client)

    # 组合提供者，未配置的部分使用 Mock
    from agent_sentinel.tools.providers.composite import CompositeToolsProvider

    return CompositeToolsProvider(
        metrics_provider=metrics_provider or MockMetricsProvider(),
        logs_provider=logs_provider or MockLogsProvider(),
        topology_provider=MockTopologyProvider(),  # 暂无拓扑 provider
    )
