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

