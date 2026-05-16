from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent_sentinel.llm.client import build_chat_model

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMExecutor:
    models: list[str]
    api_key: str = ""
    base_url: str | None = None
    temperature: float = 0.0
    trust_env: bool = False
    timeout_seconds: float = 15.0
    max_retries: int = 2
    mock_enabled: bool = False

    async def call(self, prompt: str, **kwargs: object) -> str:
        if self.mock_enabled or not self.api_key:
            logger.info("LLM mock call started prompt_chars=%s", len(prompt))
            return self._mock_response(prompt, **kwargs)

        last_exc: Exception | None = None
        for model in self.models:
            try:
                return await self._call_with_retries(model, prompt)
            except Exception as exc:  # pragma: no cover - depends on remote LLM
                last_exc = exc
                logger.exception("LLM model failed model=%s", model)

        raise RuntimeError("All LLM models failed.") from last_exc

    async def _call_with_retries(self, model_name: str, prompt: str) -> str:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                started = time.perf_counter()
                async with asyncio.timeout(self.timeout_seconds):
                    model = build_chat_model(
                        api_key=self.api_key,
                        model=model_name,
                        base_url=self.base_url,
                        temperature=self.temperature,
                        trust_env=self.trust_env,
                    )
                    response = await model.ainvoke(prompt)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                content = str(getattr(response, "content", response))
                logger.info(
                    "LLM call completed model=%s elapsed_ms=%s prompt_chars=%s response_chars=%s",
                    model_name,
                    elapsed_ms,
                    len(prompt),
                    len(content),
                )
                logger.debug("LLM response model=%s content=%s", model_name, content)
                return content

        raise RuntimeError("LLM retry loop exited unexpectedly.")

    def _mock_response(self, prompt: str, **_: object) -> str:
        lower = prompt.lower()
        if "recommended_plan" in lower or "evidence" in lower:
            return json.dumps(
                {
                    "recommended_plan": {
                        "summary": "Mock diagnosis: service latency is likely caused by downstream timeout.",
                        "actions": [
                            "Check recent timeout spikes on the order-sync dependency.",
                            "Inspect ERROR logs around the alert timestamp.",
                            "Verify upstream topology changes before rollback.",
                        ],
                        "risk_level": "medium",
                    },
                    "evidence": [
                        "Alert summary mentions timeout or failure.",
                        "Mock metrics/logs/topology were attached to the diagnosis state.",
                    ],
                    "need_human": True,
                },
                ensure_ascii=False,
            )
        if "校验" in prompt or "validate" in lower:
            return "PASS"
        return "Detected ERROR alert for order synchronization timeout. Need live metrics, logs, and topology context."
