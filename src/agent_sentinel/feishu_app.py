from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import aiohttp
import requests

from agent_sentinel.config import Settings

logger = logging.getLogger(__name__)


class FeishuBotClient:
    def __init__(self, settings: Settings, timeout: int = 15) -> None:
        self.settings = settings
        self.timeout = timeout
        self._token: str | None = None
        self._token_expire_at = 0.0
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(self.settings.feishu_app_id and self.settings.feishu_app_secret)

    def send_text_to_chat(
        self,
        chat_id: str,
        text: str,
        *,
        thread_root_message_id: str | None = None,
        mention_open_id: str | None = None,
        mention_name: str | None = None,
    ) -> bool:
        self._ensure_configured()
        token = self._get_tenant_access_token()
        base_url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages"

        final_text = text
        if mention_open_id:
            display_name = mention_name or "user"
            final_text = f'<at user_id="{mention_open_id}">@{display_name}</at> {text}'

        if thread_root_message_id:
            url = f"{base_url}/{thread_root_message_id}/reply"
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": final_text}, ensure_ascii=False),
                "reply_in_thread": True,
            }
        else:
            url = f"{base_url}?receive_id_type=chat_id"
            payload = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": final_text}, ensure_ascii=False),
            }

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu send message failed: {result}")
        return True

    def send_interactive_card_to_chat(
        self,
        chat_id: str,
        card: dict[str, object],
        *,
        thread_root_message_id: str | None = None,
    ) -> bool:
        self._ensure_configured()
        token = self._get_tenant_access_token()
        base_url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages"
        if thread_root_message_id:
            url = f"{base_url}/{thread_root_message_id}/reply"
            payload = {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
                "reply_in_thread": True,
            }
        else:
            url = f"{base_url}?receive_id_type=chat_id"
            payload = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu send card failed: {result}")
        return True

    def send_topic_text(self, chat_id: str, root_message_id: str, text: str) -> str | None:
        """Reply in the source message thread and return the new Feishu message_id."""
        self._ensure_configured()
        token = self._get_tenant_access_token()
        url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages/{root_message_id}/reply"
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "reply_in_thread": True,
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu send topic text failed: {result}")
        data = result.get("data") or {}
        return data.get("message_id") if isinstance(data, dict) else None

    def send_topic_card(self, chat_id: str, root_message_id: str, card: dict[str, object]) -> str | None:
        """Send an interactive card in the source message thread and return message_id."""
        self._ensure_configured()
        token = self._get_tenant_access_token()
        url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages/{root_message_id}/reply"
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
            "reply_in_thread": True,
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu send topic card failed: {result}")
        data = result.get("data") or {}
        return data.get("message_id") if isinstance(data, dict) else None

    def update_message_card(self, message_id: str, card: dict[str, object]) -> bool:
        """Best-effort update for a bot-sent interactive card."""
        self._ensure_configured()
        token = self._get_tenant_access_token()
        url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages/{message_id}"
        payload = {"content": json.dumps(card, ensure_ascii=False)}
        response = requests.patch(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu update message card failed: {result}")
        return True

    async def update_message_card_async(self, message_id: str, card: dict[str, object]) -> bool:
        """Async PATCH update for a bot-sent interactive card."""
        self._ensure_configured()
        token = await asyncio.to_thread(self._get_tenant_access_token)
        url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages/{message_id}"
        payload = {"content": json.dumps(card, ensure_ascii=False)}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.patch(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                result = await response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu async update message card failed: {result}")
        return True

    def list_chat_messages(
        self,
        chat_id: str,
        *,
        page_size: int = 20,
        sort_type: str = "ByCreateTimeDesc",
    ) -> list[dict[str, object]]:
        self._ensure_configured()
        token = self._get_tenant_access_token()
        url = f"{self.settings.feishu_api_base_url.rstrip('/')}/open-apis/im/v1/messages"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": page_size,
                "sort_type": sort_type,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in (0, "0", None):
            raise RuntimeError(f"Feishu list messages failed: {result}")
        data = result.get("data") or {}
        items = data.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError("FEISHU_APP_ID or FEISHU_APP_SECRET is not configured.")

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        with self._lock:
            if self._token and now < self._token_expire_at:
                return self._token

            url = (
                f"{self.settings.feishu_api_base_url.rstrip('/')}"
                "/open-apis/auth/v3/tenant_access_token/internal"
            )
            response = requests.post(
                url,
                json={
                    "app_id": self.settings.feishu_app_id,
                    "app_secret": self.settings.feishu_app_secret,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") not in (0, "0", None):
                raise RuntimeError(f"Feishu tenant_access_token fetch failed: {result}")

            token = result.get("tenant_access_token")
            if not token:
                raise RuntimeError("Feishu tenant_access_token is missing in response.")

            expire = int(result.get("expire", 7200))
            self._token = token
            self._token_expire_at = now + max(expire - 120, 60)
            return token


def extract_text_from_message_content(content: str | None) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text
    return content
