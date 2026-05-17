from __future__ import annotations

import asyncio

from agent_sentinel.interactive_topic.workflow import InteractiveTopicWorkflow
from agent_sentinel.rag.models import RetrievedDoc


class FakeSender:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    async def update_workflow_card(self, message_id: str | None, **kwargs: object) -> bool:
        self.updates.append({"message_id": message_id, **kwargs})
        return True


class FakeRetriever:
    async def retrieve(self, query, filters=None):
        return [
            RetrievedDoc(
                id="doc-1",
                text=f"真实检索片段：{query} 可能与 MQ 堆积有关。",
                source_type="static_doc",
                score=0.91,
                title="消息堆积排查",
                source_uri="data/static_docs/manual.md",
                metadata={
                    "section": "第一章_消息队列_1.1_消息堆积",
                    "alert_category": "MQ",
                    "severity_level": "P0",
                },
            )
        ]


class MultiDocRetriever:
    async def retrieve(self, query, filters=None):
        return [
            RetrievedDoc(id=f"doc-{index}", text=f"片段 {index}", source_type="static_doc", score=0.9 - index / 100)
            for index in range(1, 4)
        ]


class FakeCaseStore:
    def __init__(self) -> None:
        self.saved_state = None

    async def search_similar_cases(self, alert_text, *, top_k=2, threshold=None):
        return [
            RetrievedDoc(
                id="alert-case-1",
                text=alert_text,
                source_type="message_history",
                score=0.96,
                title="CPU 飙升成功案例",
                metadata={"recommended_plan": {"summary": "扩容 worker"}},
            )
        ]

    async def save_case_to_history(self, state):
        self.saved_state = state
        return "saved-case-1"


class FailingCaseStore(FakeCaseStore):
    async def save_case_to_history(self, state):
        self.saved_state = state
        raise RuntimeError("milvus varchar too long")


def test_interactive_topic_routes_retry_twice_to_next() -> None:
    workflow = InteractiveTopicWorkflow(sender=FakeSender())  # type: ignore[arg-type]

    route = workflow._route_after_confirm(  # noqa: SLF001
        {
            "current_node": "rag_retrieve",
            "last_action": "retry",
            "retry_counts": {"rag_retrieve": 2},
        }
    )

    assert route == "next"


def test_interactive_topic_failed_node_is_skipped() -> None:
    async def run() -> None:
        sender = FakeSender()
        workflow = InteractiveTopicWorkflow(sender=sender)  # type: ignore[arg-type]

        async def failing_runner(_state):
            raise RuntimeError("boom")

        result = await workflow._interactive_node(  # noqa: SLF001
            "rag_retrieve",
            failing_runner,
            {
                "task_id": "topic-1",
                "query": "CPU 飙升",
                "node_results": [],
                "retry_counts": {},
            },
        )

        assert result["last_action"] == "skip"
        assert "执行失败" in result["node_result"]
        assert sender.updates[-1]["node_statuses"] == {"rag_retrieve": "skipped"}

    asyncio.run(run())


def test_interactive_topic_rag_uses_real_retriever() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=FakeRetriever(),  # type: ignore[arg-type]
        )

        result = await workflow._rag_retrieve({"query": "订单消息堆积"})  # noqa: SLF001

        assert "文档/混合召回 Top 1 / 共命中 1 条" in result
        assert "消息堆积排查" in result
        assert "section=第一章_消息队列_1.1_消息堆积" in result
        assert "真实检索片段" in result

    asyncio.run(run())


def test_interactive_topic_cache_check_uses_history_case_store() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            case_store=FakeCaseStore(),  # type: ignore[arg-type]
        )

        result = await workflow._cache_check({"query": "CPU 飙升"})  # noqa: SLF001

        assert "命中 1 条可复用历史成功案例" in result
        assert "alert-case-1" in result
        assert "扩容 worker" in result

    asyncio.run(run())


def test_interactive_topic_rag_displays_top2_only() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=MultiDocRetriever(),  # type: ignore[arg-type]
        )

        result = await workflow._rag_retrieve({"query": "CPU 飙升"})  # noqa: SLF001

        assert "文档/混合召回 Top 2 / 共命中 3 条" in result
        assert "doc-1" in result
        assert "doc-2" in result
        assert "doc-3" not in result

    asyncio.run(run())


def test_interactive_topic_rag_also_shows_history_cases() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=FakeRetriever(),  # type: ignore[arg-type]
            case_store=FakeCaseStore(),  # type: ignore[arg-type]
        )

        result = await workflow._rag_retrieve({"query": "CPU 飙升"})  # noqa: SLF001

        assert "文档/混合召回 Top 1 / 共命中 1 条" in result
        assert "历史案例召回 Top 1" in result
        assert "alert-case-1" in result
        assert "扩容 worker" in result

    asyncio.run(run())


def test_interactive_topic_feedback_valid_saves_case() -> None:
    async def run() -> None:
        sender = FakeSender()
        case_store = FakeCaseStore()
        workflow = InteractiveTopicWorkflow(
            sender=sender,  # type: ignore[arg-type]
            case_store=case_store,  # type: ignore[arg-type]
        )
        task = workflow.task_store.create_task("topic-1", "oc-chat", "om-root", "CPU 飙升")
        workflow.task_store.set_card_message_id(task.task_id, "om-card")
        workflow.task_store.mark_feedback_waiting(task.task_id, "om-card")
        app = workflow.compile()
        await app.aupdate_state(
            {"configurable": {"thread_id": task.task_id}, "recursion_limit": 50},
            {
                "query": "CPU 飙升",
                "task_id": task.task_id,
                "node_results": [
                    {"node_name": "summary", "result": "建议扩容 worker"},
                ],
            },
        )

        result = await workflow.handle_card_callback(
            {"value": {"task_id": task.task_id, "node_name": "feedback_learning", "action": "feedback_valid"}}
        )

        assert result == {"status": "ok", "saved_case_id": "saved-case-1"}
        assert case_store.saved_state["recommended_plan"]["summary"] == "建议扩容 worker"
        assert case_store.saved_state["alert_summary"] == "CPU 飙升"
        assert "raw_text" not in case_store.saved_state["raw_alert"]
        assert sender.updates[-1]["feedback_buttons"] is False
        assert "已存入历史案例知识库" in sender.updates[-1]["current_result"]

    asyncio.run(run())


def test_interactive_topic_feedback_save_failure_can_retry() -> None:
    async def run() -> None:
        sender = FakeSender()
        workflow = InteractiveTopicWorkflow(
            sender=sender,  # type: ignore[arg-type]
            case_store=FailingCaseStore(),  # type: ignore[arg-type]
        )
        task = workflow.task_store.create_task("topic-2", "oc-chat", "om-root", "CPU 飙升")
        workflow.task_store.set_card_message_id(task.task_id, "om-card")
        workflow.task_store.mark_feedback_waiting(task.task_id, "om-card")
        app = workflow.compile()
        await app.aupdate_state(
            {"configurable": {"thread_id": task.task_id}, "recursion_limit": 50},
            {
                "query": "CPU 飙升",
                "task_id": task.task_id,
                "node_results": [
                    {"node_name": "summary", "result": "建议扩容 worker"},
                ],
            },
        )

        result = await workflow.handle_card_callback(
            {"value": {"task_id": task.task_id, "node_name": "feedback_learning", "action": "feedback_valid"}}
        )

        assert result == {"status": "error", "reason": "save_failed"}
        assert workflow.task_store.get_task(task.task_id).status == "pending_feedback"
        assert sender.updates[-1]["feedback_buttons"] is True
        assert "入库失败" in sender.updates[-1]["current_result"]

    asyncio.run(run())
