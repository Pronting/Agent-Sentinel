from __future__ import annotations

import asyncio

from agent_sentinel.config import Settings
from agent_sentinel.feishu.card_handler import HumanDecisionStore
from agent_sentinel.feishu.sender import FeishuSender
from agent_sentinel.graph.workflow import DiagnosisWorkflow, build_llm_executor


def make_settings() -> Settings:
    return Settings(
        app_name="Agent Sentinel",
        app_host="127.0.0.1",
        app_port=8000,
        app_env="test",
        log_level="INFO",
        openai_api_key="",
        openai_base_url=None,
        openai_model="gpt-4o-mini",
        openai_temperature=0.0,
        openai_http_trust_env=False,
        feishu_api_base_url="https://open.feishu.cn",
        feishu_app_id=None,
        feishu_app_secret=None,
        feishu_event_verification_token=None,
        feishu_event_encrypt_key=None,
        feishu_bot_name="Analysis Bot",
        feishu_allowed_chat_ids=[],
        feishu_analyze_mention_only=True,
        feishu_long_connection_enabled=False,
        feishu_message_polling_enabled=False,
        feishu_message_polling_interval_seconds=5,
        feishu_message_polling_page_size=20,
        feishu_webhook_url=None,
        feishu_secret=None,
        feishu_alert_enabled=False,
        feishu_alert_title_prefix="Agent Sentinel",
        alert_analysis_enabled=True,
        alert_analysis_title_prefix="Alert Analysis",
        alert_api_token="test-token",
        alert_dedup_window_seconds=60,
        alert_store_limit=100,
        aiops_mock_llm_enabled=True,
        aiops_human_confirm_enabled=False,
        redis_url=None,
    )


def test_langgraph_workflow_runs_mock_diagnosis() -> None:
    async def run() -> None:
        settings = make_settings()
        workflow = DiagnosisWorkflow(
            settings=settings,
            llm=build_llm_executor(settings),
            sender=FeishuSender(None),
            decision_store=HumanDecisionStore(None),
        )
        final_state = await workflow.run_streaming(
            {
                "raw_alert": {
                    "source": "unit-test",
                    "level": "ERROR",
                    "summary": "order sync timeout",
                    "details": "timeout after 3 retries",
                },
                "chat_id": "",
                "messages": [],
                "evidence": [],
                "retrieved_docs": [],
                "live_data": {},
                "recommended_plan": {},
                "validation_result": False,
                "need_human": True,
            }
        )
        assert final_state["alert_summary"]
        assert final_state["recommended_plan"]
        assert final_state["evidence"]
        assert final_state["validation_result"] is True
        assert final_state["human_decision"] == "approved"
        assert "AIOps Diagnosis" in final_state["final_text"]

    asyncio.run(run())
