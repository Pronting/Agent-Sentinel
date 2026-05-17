from __future__ import annotations

import asyncio

from agent_sentinel.agents.feedback_learning import feedback_learning_node
from agent_sentinel.agents.understand_agent import understand_node
from agent_sentinel.feishu.card_handler import DecisionContext, FeishuCardHandler, HumanDecisionStore
from agent_sentinel.feishu.sender import FeishuSender, build_case_cache_card, build_feedback_card
from agent_sentinel.rag.history_cases import HistoryCaseStore
from agent_sentinel.rag.models import RetrievedDoc


class FakeLLM:
    async def call(self, prompt: str) -> str:
        return "CPU 飙升，疑似线程池耗尽"


class FakeCaseStore:
    def __init__(self) -> None:
        self.saved = False

    async def search_similar_cases(self, alert_text: str, *, top_k: int = 2, threshold: float | None = None):
        return [
            RetrievedDoc(
                id="case-1",
                text="历史 CPU 告警",
                source_type="message_history",
                score=0.93,
                title="CPU 飙升历史案例",
                metadata={"recommended_plan": {"summary": "重启连接池并扩容 worker"}},
            )
        ]

    async def save_case_to_history(self, state):
        self.saved = True
        return "case-saved-1"


class FakeEmbedding:
    async def embed(self, text: str):
        return [0.1, 0.2]


class CapturingMilvus:
    def __init__(self) -> None:
        self.records = []

    async def ensure_static_doc_collection(self, collection_name: str, dimension: int) -> None:
        return None

    async def upsert(self, collection_name: str, records):
        self.records.extend(records)


def test_understand_node_adopts_history_case_without_downstream_nodes() -> None:
    async def run() -> None:
        decision_store = HumanDecisionStore(None)
        await decision_store.register_pending_decision(
            DecisionContext(
                decision_id="wf-1:case-cache:0",
                workflow_thread_id="wf-1",
                workflow_run_id="run-1",
                status="adopt",
            )
        )

        state = await understand_node(
            {
                "raw_alert": {"summary": "CPU 飙升", "details": "worker timeout"},
                "chat_id": "oc_chat",
                "workflow_thread_id": "wf-1",
                "workflow_run_id": "run-1",
                "messages": [],
                "evidence": [],
            },
            llm=FakeLLM(),
            case_store=FakeCaseStore(),
            sender=FeishuSender(None),
            decision_store=decision_store,
        )

        assert state["cache_hit"] is True
        assert state["human_decision"] == "cache_approved"
        assert state["need_human"] is False
        assert state["recommended_plan"]["summary"] == "重启连接池并扩容 worker"

    asyncio.run(run())


def test_feedback_learning_saves_valid_case() -> None:
    async def run() -> None:
        decision_store = HumanDecisionStore(None)
        await decision_store.register_pending_decision(
            DecisionContext(
                decision_id="wf-2:case-feedback",
                workflow_thread_id="wf-2",
                workflow_run_id="run-2",
                status="valid",
            )
        )
        case_store = FakeCaseStore()

        state = await feedback_learning_node(
            {
                "raw_alert": {"summary": "MQ 堆积"},
                "chat_id": "oc_chat",
                "workflow_thread_id": "wf-2",
                "workflow_run_id": "run-2",
                "recommended_plan": {"summary": "扩容消费者"},
                "evidence": [],
                "messages": [],
            },
            sender=FeishuSender(None),
            decision_store=decision_store,
            case_store=case_store,
            enabled=True,
        )

        assert case_store.saved is True
        assert state["feedback_decision"] == "valid"
        assert state["saved_case_id"] == "case-saved-1"

    asyncio.run(run())


def test_card_handler_parses_cache_and_feedback_actions() -> None:
    async def run() -> None:
        decision_store = HumanDecisionStore(None)
        await decision_store.register_pending_decision(
            DecisionContext("cache-1", "wf-1", "run-1")
        )
        await decision_store.register_pending_decision(
            DecisionContext("feedback-1", "wf-1", "run-1")
        )
        handler = FeishuCardHandler(decision_store)

        cache_context = await handler.parse_callback(
            {"action": {"value": {"action": "case_cache_decision", "decision": "adopt", "decision_id": "cache-1"}}}
        )
        feedback_context = await handler.parse_callback(
            {"action": {"value": {"action": "case_feedback", "decision": "valid", "decision_id": "feedback-1"}}}
        )

        assert cache_context is not None
        assert cache_context.status == "adopt"
        assert feedback_context is not None
        assert feedback_context.status == "valid"

    asyncio.run(run())


def test_new_cards_include_expected_buttons() -> None:
    cache_card = build_case_cache_card(
        "decision-1",
        "wf-1",
        "run-1",
        {"title": "CPU 飙升历史案例", "score": 0.91, "final_plan": {"summary": "扩容"}},
        candidate_index=0,
    )
    feedback_card = build_feedback_card("decision-2", "wf-1", "run-1", {"summary": "扩容"}, ["evidence"])

    cache_buttons = [item["text"]["content"] for item in cache_card["elements"][-1]["actions"]]
    feedback_buttons = [item["text"]["content"] for item in feedback_card["elements"][-1]["actions"]]
    assert cache_buttons == ["✅ 采用此方案", "❌ 不采用"]
    assert feedback_buttons == ["✅ 有效，存入知识库", "❌ 无效，不存储"]


def test_history_case_store_truncates_milvus_varchar_fields_by_bytes() -> None:
    async def run() -> None:
        milvus = CapturingMilvus()
        store = HistoryCaseStore(
            milvus=milvus,  # type: ignore[arg-type]
            embedding=FakeEmbedding(),  # type: ignore[arg-type]
            collection_name="aiops_message_history",
        )

        await store.save_case_to_history(
            {
                "raw_alert": {
                    "summary": "故障" * 400,
                    "source": "服务" * 100,
                    "tags": ["缓存" * 300],
                },
                "alert_summary": "节点输出" * 400,
                "retrieved_docs": ["文档" * 5000],
                "recommended_plan": {"summary": "方案" * 2000},
                "evidence": ["证据" * 2000],
                "messages": [{"role": "assistant", "content": "日志" * 5000}],
            }
        )

        record = milvus.records[0]
        assert len(record["title"].encode("utf-8")) <= 512
        assert len(record["service"].encode("utf-8")) <= 128
        assert len(record["tags"].encode("utf-8")) <= 1024
        assert len(record["metadata"].encode("utf-8")) <= 8192

    asyncio.run(run())
