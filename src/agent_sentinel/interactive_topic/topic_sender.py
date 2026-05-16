from __future__ import annotations

import asyncio
import logging

from agent_sentinel.feishu_app import FeishuBotClient

logger = logging.getLogger(__name__)


class InteractiveTopicSender:
    """Send text and confirmation cards into a Feishu message thread."""

    def __init__(self, client: FeishuBotClient | None, wait_seconds: int = 5) -> None:
        self.client = client
        self.wait_seconds = wait_seconds

    async def send_topic_text(self, chat_id: str, root_message_id: str, text: str) -> str | None:
        if not self.client or not self.client.is_configured():
            logger.info("Interactive topic text skipped chat_id=%s text=%s", chat_id, text)
            return None
        try:
            return await asyncio.to_thread(
                self.client.send_topic_text,
                chat_id,
                root_message_id,
                text,
            )
        except Exception:
            logger.exception("Failed to send interactive topic text chat_id=%s root=%s", chat_id, root_message_id)
            return None

    async def send_topic_card(
        self,
        chat_id: str,
        root_message_id: str,
        task_id: str,
        node_name: str,
        node_result: str,
    ) -> str | None:
        if not self.client or not self.client.is_configured():
            logger.info("Interactive topic card skipped chat_id=%s task_id=%s node=%s", chat_id, task_id, node_name)
            return None
        card = build_topic_confirm_card(
            task_id=task_id,
            node_name=node_name,
            node_result=node_result,
            wait_seconds=self.wait_seconds,
            status_text="等待确认",
            buttons_enabled=True,
        )
        try:
            return await asyncio.to_thread(
                self.client.send_topic_card,
                chat_id,
                root_message_id,
                card,
            )
        except Exception:
            logger.exception(
                "Failed to send interactive topic card chat_id=%s root=%s task_id=%s node=%s",
                chat_id,
                root_message_id,
                task_id,
                node_name,
            )
            return None

    async def update_topic_card_status(
        self,
        message_id: str | None,
        task_id: str,
        node_name: str,
        node_result: str,
        status_text: str,
    ) -> bool:
        if not message_id or not self.client or not self.client.is_configured():
            return False
        card = build_topic_confirm_card(
            task_id=task_id,
            node_name=node_name,
            node_result=node_result,
            wait_seconds=self.wait_seconds,
            status_text=status_text,
            buttons_enabled=False,
        )
        try:
            return await asyncio.to_thread(self.client.update_message_card, message_id, card)
        except Exception:
            logger.exception("Failed to update interactive topic card message_id=%s", message_id)
            return False


def build_topic_confirm_card(
    *,
    task_id: str,
    node_name: str,
    node_result: str,
    wait_seconds: int,
    status_text: str,
    buttons_enabled: bool,
) -> dict[str, object]:
    actions: list[dict[str, object]] = []
    if buttons_enabled:
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Yes 下一步"},
                "type": "primary",
                "value": {"task_id": task_id, "node_name": node_name, "action": "next"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "No 重试节点"},
                "type": "danger",
                "value": {"task_id": task_id, "node_name": node_name, "action": "retry"},
            },
        ]
    else:
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": status_text},
                "type": "default",
                "disabled": True,
                "value": {"task_id": task_id, "node_name": node_name, "action": "noop"},
            }
        ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "LangGraph 节点确认"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**当前节点：{node_name}**\n\n"
                    f"{node_result}\n\n"
                    f"**状态：{status_text}**\n\n"
                    f"{wait_seconds} 秒无操作自动进入下一步"
                ),
            },
            {"tag": "action", "actions": actions},
        ],
    }
