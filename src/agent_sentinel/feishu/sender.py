from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_sentinel.feishu_app import FeishuBotClient

logger = logging.getLogger(__name__)


class FeishuSender:
    def __init__(self, client: FeishuBotClient | None = None) -> None:
        self.client = client

    async def send_message(
        self,
        chat_id: str | None,
        text: str,
        *,
        thread_root_message_id: str | None = None,
        mention_open_id: str | None = None,
        mention_name: str | None = None,
    ) -> bool:
        if not chat_id or not self.client or not self.client.is_configured():
            logger.info("Feishu text skipped chat_id=%s text=%s", chat_id, text)
            return False
        return await asyncio.to_thread(
            self.client.send_text_to_chat,
            chat_id,
            text,
            thread_root_message_id=thread_root_message_id,
            mention_open_id=mention_open_id,
            mention_name=mention_name,
        )

    async def send_card(
        self,
        chat_id: str | None,
        decision_id: str,
        workflow_thread_id: str,
        workflow_run_id: str,
        plan: dict[str, Any],
        evidence: list[str],
        *,
        thread_root_message_id: str | None = None,
    ) -> bool:
        if not chat_id or not self.client or not self.client.is_configured():
            logger.info("Feishu card skipped chat_id=%s decision_id=%s", chat_id, decision_id)
            return False
        card = build_confirmation_card(decision_id, workflow_thread_id, workflow_run_id, plan, evidence)
        return await asyncio.to_thread(
            self.client.send_interactive_card_to_chat,
            chat_id,
            card,
            thread_root_message_id=thread_root_message_id,
        )


def build_confirmation_card(
    decision_id: str,
    workflow_thread_id: str,
    workflow_run_id: str,
    plan: dict[str, Any],
    evidence: list[str],
) -> dict[str, Any]:
    plan_summary = str(plan.get("summary") or plan)[:900]
    evidence_text = "\n".join(f"- {item}" for item in evidence[:8]) or "- no evidence"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "AIOps 告警诊断确认"},
        },
        "elements": [
            {"tag": "markdown", "content": f"**推荐方案**\n{plan_summary}"},
            {"tag": "markdown", "content": f"**证据链**\n{evidence_text}"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "同意"},
                        "type": "primary",
                        "value": {
                            "action": "diagnosis_confirm",
                            "decision": "approved",
                            "decision_id": decision_id,
                            "workflow_thread_id": workflow_thread_id,
                            "workflow_run_id": workflow_run_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "diagnosis_confirm",
                            "decision": "rejected",
                            "decision_id": decision_id,
                            "workflow_thread_id": workflow_thread_id,
                            "workflow_run_id": workflow_run_id,
                        },
                    },
                ],
            },
        ],
    }
