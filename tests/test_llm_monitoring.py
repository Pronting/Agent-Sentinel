from __future__ import annotations

import asyncio

from agent_sentinel.llm.executor import LLMExecutor
from agent_sentinel.monitoring import configure_monitoring, monitor


def test_llm_executor_records_token_consumption_metric() -> None:
    async def run() -> None:
        configure_monitoring(enabled=True)
        executor = LLMExecutor(models=["mock"], mock_enabled=True)

        await executor.call("Summarize database timeout evidence.")

        metrics = monitor.render_latest().decode("utf-8")
        assert 'llm_call_duration_seconds_count{model="mock"}' in metrics
        assert 'token_consumption_total{model="mock",token_type="prompt"}' in metrics
        assert 'token_consumption_total{model="mock",token_type="completion"}' in metrics

    asyncio.run(run())
