from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HumanDecision:
    decision: str
    feedback: str = ""


class HumanDecisionStore:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url
        self._futures: dict[str, asyncio.Future[HumanDecision]] = {}
        self._redis: redis.Redis | None = None

    async def open(self) -> None:
        if self.redis_url and self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)

    async def register(self, decision_id: str) -> None:
        await self.open()
        loop = asyncio.get_running_loop()
        self._futures.setdefault(decision_id, loop.create_future())
        if self._redis:
            await self._redis.setex(f"decision:{decision_id}:status", 600, "pending")

    async def set_decision(self, decision_id: str, decision: str, feedback: str = "") -> None:
        payload = HumanDecision(decision=decision, feedback=feedback)
        future = self._futures.get(decision_id)
        if future and not future.done():
            future.set_result(payload)
        if self._redis:
            await self._redis.setex(
                f"decision:{decision_id}:result",
                600,
                json.dumps({"decision": decision, "feedback": feedback}, ensure_ascii=False),
            )
        logger.info("Human decision recorded decision_id=%s decision=%s", decision_id, decision)

    async def wait_for_decision(self, decision_id: str, timeout_seconds: int) -> HumanDecision:
        await self.register(decision_id)
        future = self._futures[decision_id]
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=1)
            except asyncio.TimeoutError:
                if self._redis:
                    raw = await self._redis.get(f"decision:{decision_id}:result")
                    if raw:
                        data = json.loads(raw)
                        return HumanDecision(
                            decision=str(data.get("decision", "timeout")),
                            feedback=str(data.get("feedback", "")),
                        )
                if time.monotonic() >= deadline:
                    logger.info("Human decision timed out decision_id=%s", decision_id)
                    return HumanDecision(decision="timeout", feedback="confirmation timed out")


class FeishuCardHandler:
    def __init__(self, decision_store: HumanDecisionStore) -> None:
        self.decision_store = decision_store

    async def handle(self, payload: dict[str, Any]) -> dict[str, str]:
        value = self._extract_value(payload)
        if value.get("action") != "diagnosis_confirm":
            return {"status": "ignored"}
        decision_id = str(value.get("decision_id") or "")
        decision = str(value.get("decision") or "")
        feedback = str(value.get("feedback") or payload.get("feedback") or "")
        if not decision_id or decision not in {"approved", "rejected"}:
            return {"status": "ignored"}
        await self.decision_store.set_decision(decision_id, decision, feedback)
        return {"status": "ok"}

    def _extract_value(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if isinstance(action, dict):
            value = action.get("value")
            if isinstance(value, dict):
                return value
        value = payload.get("value")
        if isinstance(value, dict):
            return value
        return payload
