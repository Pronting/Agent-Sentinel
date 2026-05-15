from __future__ import annotations

import importlib.util
import json
import logging
import threading
from typing import Callable

from agent_sentinel.config import Settings
from agent_sentinel.feishu_app import extract_text_from_message_content

logger = logging.getLogger(__name__)


AnalyzeCallback = Callable[
    [str, str, str, str, str | None, str | None, str, list[str] | None, str | None, str | None, str | None],
    tuple[str, bool],
]


class FeishuLongConnectionBot:
    def __init__(self, settings: Settings, analyze_callback: AnalyzeCallback) -> None:
        self.settings = settings
        self.analyze_callback = analyze_callback
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if not self.settings.feishu_long_connection_enabled:
            logger.info("Feishu long connection listener is disabled.")
            return
        if not (self.settings.feishu_app_id and self.settings.feishu_app_secret):
            logger.info("Feishu long connection listener skipped because app credentials are missing.")
            return
        if importlib.util.find_spec("lark_oapi") is None:
            logger.warning(
                "Feishu long connection listener skipped because lark_oapi is not installed. "
                "Run `pip install lark-oapi` or reinstall project dependencies."
            )
            return

        self._thread = threading.Thread(
            target=self._run_forever,
            name="feishu-long-connection",
            daemon=True,
        )
        self._thread.start()
        self._started = True

    def _run_forever(self) -> None:
        import lark_oapi as lark

        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.settings.feishu_event_encrypt_key or "",
                self.settings.feishu_event_verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )

        ws_client = lark.ws.Client(
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        logger.info("Starting Feishu long connection listener.")
        ws_client.start()

    def _handle_message_event(self, data: object) -> None:
        try:
            import lark_oapi as lark

            payload = json.loads(lark.JSON.marshal(data))
            event = payload.get("event") or {}
            sender = event.get("sender") or {}
            if sender.get("sender_type") != "user":
                return

            message = event.get("message") or {}
            chat_id = str(message.get("chat_id") or "")
            if not chat_id:
                return

            if self.settings.feishu_allowed_chat_ids and chat_id not in self.settings.feishu_allowed_chat_ids:
                return

            chat_type = str(message.get("chat_type") or "")
            mentions = message.get("mentions") or []
            if self.settings.feishu_analyze_mention_only and chat_type != "p2p" and not mentions:
                return

            content_text = extract_text_from_message_content(message.get("content"))
            if not content_text.strip():
                return

            self.analyze_callback(
                chat_id,
                "feishu-user",
                "INFO",
                f"{self.settings.feishu_bot_name} received a message",
                content_text,
                content_text,
                "user_message",
                ["feishu", "long-connection"],
                str(message.get("root_id") or "") or str(message.get("message_id") or "") or None,
                self._extract_sender_open_id(sender),
                self._extract_sender_name(sender),
            )
        except Exception:
            logger.exception("Failed to process Feishu long connection message event")

    def _extract_sender_open_id(self, sender: object) -> str | None:
        if not isinstance(sender, dict):
            return None
        sender_id = str(sender.get("sender_id") or sender.get("id") or "").strip()
        return sender_id or None

    def _extract_sender_name(self, sender: object) -> str | None:
        if not isinstance(sender, dict):
            return None
        name = str(sender.get("name") or "").strip()
        return name or None
