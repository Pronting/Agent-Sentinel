from __future__ import annotations

from agent_sentinel.config import Settings
from agent_sentinel.rag.factory import build_retriever
from agent_sentinel.rag.mmr import select_mmr
from agent_sentinel.rag.mock_retriever import MockRetriever
from agent_sentinel.rag.models import RetrievedDoc


def make_settings(**overrides: object) -> Settings:
    values = dict(
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
    )
    values.update(overrides)
    return Settings(**values)


def test_mmr_prefers_relevance_and_diversity() -> None:
    docs = [
        RetrievedDoc(
            id="a",
            text="payment timeout",
            source_type="static_doc",
            score=0.9,
            weighted_score=0.9,
            embedding=[1.0, 0.0],
        ),
        RetrievedDoc(
            id="b",
            text="payment timeout duplicate",
            source_type="message_history",
            score=0.88,
            weighted_score=0.88,
            embedding=[0.98, 0.02],
        ),
        RetrievedDoc(
            id="c",
            text="mysql connection pool",
            source_type="static_doc",
            score=0.72,
            weighted_score=0.72,
            embedding=[0.0, 1.0],
        ),
    ]

    selected = select_mmr(query_embedding=[1.0, 0.0], docs=docs, top_k=2, lambda_mult=0.5)

    assert [doc.id for doc in selected] == ["a", "c"]


def test_milvus_provider_without_uri_falls_back_to_mock() -> None:
    retriever = build_retriever(make_settings(rag_provider="milvus", milvus_uri=None))

    assert isinstance(retriever, MockRetriever)
