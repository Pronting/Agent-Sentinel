from __future__ import annotations

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from agent_sentinel.config import Settings
from agent_sentinel.llm import build_chat_model


class SingleTurnChatService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = build_chat_model(settings)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are Agent Sentinel, a concise and helpful assistant. "
                    "Answer the user's latest message clearly and directly.",
                ),
                ("human", "{user_input}"),
            ]
        )
        self.chain = self.prompt | self.model | StrOutputParser()

    def reply_once(self, user_input: str) -> str:
        return self.chain.invoke({"user_input": user_input})


class AlertAnalysisService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = build_chat_model(settings)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are an incident analysis assistant. "
                    "Reply in concise Chinese, but keep the section labels exactly as below. "
                    "Use one short line per item and avoid filler.\n"
                    "[Alert Analysis]\n"
                    "Severity: ...\n"
                    "Possible Cause: ...\n"
                    "Impact: ...\n"
                    "Suggested Action: ...\n"
                    "Needs Human Follow-up: Yes/No + reason",
                ),
                (
                    "human",
                    "Source: {source}\n"
                    "Level: {level}\n"
                    "Summary: {summary}\n"
                    "Details: {details}\n"
                    "Raw Text: {raw_text}\n"
                    "Trigger Type: {trigger_type}\n"
                    "Tags: {tags}",
                ),
            ]
        )
        self.chain = self.prompt | self.model | StrOutputParser()

    def analyze_alert(
        self,
        source: str,
        level: str,
        summary: str,
        details: str | None = None,
        raw_text: str | None = None,
        trigger_type: str = "unknown",
        tags: list[str] | None = None,
    ) -> str:
        return self.chain.invoke(
            {
                "source": source,
                "level": level,
                "summary": summary,
                "details": details or "",
                "raw_text": raw_text or "",
                "trigger_type": trigger_type,
                "tags": ", ".join(tags or []),
            }
        )
