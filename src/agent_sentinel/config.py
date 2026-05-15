from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _to_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _to_list(value: str | None) -> list[str]:
    if value is None or value.strip() == "":
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None


@dataclass(slots=True)
class Settings:
    app_name: str
    app_host: str
    app_port: int
    app_env: str
    log_level: str
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    openai_temperature: float
    openai_http_trust_env: bool
    feishu_api_base_url: str
    feishu_app_id: str | None
    feishu_app_secret: str | None
    feishu_event_verification_token: str | None
    feishu_event_encrypt_key: str | None
    feishu_bot_name: str
    feishu_allowed_chat_ids: list[str]
    feishu_analyze_mention_only: bool
    feishu_long_connection_enabled: bool
    feishu_message_polling_enabled: bool
    feishu_message_polling_interval_seconds: int
    feishu_message_polling_page_size: int
    feishu_webhook_url: str | None
    feishu_secret: str | None
    feishu_alert_enabled: bool
    feishu_alert_title_prefix: str
    alert_analysis_enabled: bool
    alert_analysis_title_prefix: str
    alert_api_token: str | None
    alert_dedup_window_seconds: int
    alert_store_limit: int


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        app_name=os.getenv("APP_NAME", "Agent Sentinel"),
        app_host=os.getenv("APP_HOST", "0.0.0.0"),
        app_port=_to_int(os.getenv("APP_PORT"), 8000),
        app_env=os.getenv("APP_ENV", "dev"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=_first_non_empty(
            os.getenv("CODEX_BASE_URL"),
            os.getenv("OPENAI_BASE_URL"),
        ),
        openai_model=_first_non_empty(
            os.getenv("CODEX_MODEL"),
            os.getenv("OPENAI_MODEL"),
        )
        or "gpt-4o-mini",
        openai_temperature=_to_float(os.getenv("OPENAI_TEMPERATURE"), 0.0),
        openai_http_trust_env=_to_bool(os.getenv("OPENAI_HTTP_TRUST_ENV"), False),
        feishu_api_base_url=os.getenv("FEISHU_API_BASE_URL", "https://open.feishu.cn"),
        feishu_app_id=os.getenv("FEISHU_APP_ID"),
        feishu_app_secret=os.getenv("FEISHU_APP_SECRET"),
        feishu_event_verification_token=os.getenv("FEISHU_EVENT_VERIFICATION_TOKEN"),
        feishu_event_encrypt_key=os.getenv("FEISHU_EVENT_ENCRYPT_KEY"),
        feishu_bot_name=os.getenv("FEISHU_BOT_NAME", "Analysis Bot"),
        feishu_allowed_chat_ids=_to_list(os.getenv("FEISHU_ALLOWED_CHAT_IDS")),
        feishu_analyze_mention_only=_to_bool(os.getenv("FEISHU_ANALYZE_MENTION_ONLY"), True),
        feishu_long_connection_enabled=_to_bool(os.getenv("FEISHU_LONG_CONNECTION_ENABLED"), True),
        feishu_message_polling_enabled=_to_bool(os.getenv("FEISHU_MESSAGE_POLLING_ENABLED"), False),
        feishu_message_polling_interval_seconds=_to_int(
            os.getenv("FEISHU_MESSAGE_POLLING_INTERVAL_SECONDS"),
            5,
        ),
        feishu_message_polling_page_size=_to_int(
            os.getenv("FEISHU_MESSAGE_POLLING_PAGE_SIZE"),
            20,
        ),
        feishu_webhook_url=os.getenv("FEISHU_WEBHOOK_URL"),
        feishu_secret=os.getenv("FEISHU_SECRET"),
        feishu_alert_enabled=_to_bool(os.getenv("FEISHU_ALERT_ENABLED"), True),
        feishu_alert_title_prefix=os.getenv("FEISHU_ALERT_TITLE_PREFIX", "Agent Sentinel"),
        alert_analysis_enabled=_to_bool(os.getenv("ALERT_ANALYSIS_ENABLED"), True),
        alert_analysis_title_prefix=os.getenv("ALERT_ANALYSIS_TITLE_PREFIX", "Alert Analysis"),
        alert_api_token=os.getenv("ALERT_API_TOKEN"),
        alert_dedup_window_seconds=_to_int(os.getenv("ALERT_DEDUP_WINDOW_SECONDS"), 60),
        alert_store_limit=_to_int(os.getenv("ALERT_STORE_LIMIT"), 100),
    )
