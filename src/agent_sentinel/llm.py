from __future__ import annotations

import httpx
from langchain_openai import ChatOpenAI

from agent_sentinel.config import Settings


def build_chat_model(settings: Settings) -> ChatOpenAI:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is missing. Please set it in your .env file.")

    kwargs: dict[str, object] = {
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
        "temperature": settings.openai_temperature,
        # Use explicit clients so we control whether httpx trusts system/env proxy
        # settings. This avoids machine-specific proxy auto-detection causing
        # connection failures for Codex-compatible endpoints.
        "http_client": httpx.Client(trust_env=settings.openai_http_trust_env),
        "http_async_client": httpx.AsyncClient(trust_env=settings.openai_http_trust_env),
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url

    return ChatOpenAI(**kwargs)
