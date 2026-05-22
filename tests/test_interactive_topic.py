from __future__ import annotations

import asyncio

from agent_sentinel.interactive_topic.topic_sender import WORKFLOW_STEPS
from agent_sentinel.interactive_topic.workflow import InteractiveTopicWorkflow, _build_summary_prompt
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


class FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def call(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        return "LLM 诊断总结"


class WorkflowLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def call(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        if len(self.prompts) == 3:
            return "PASS"
        if len(self.prompts) == 2:
            return (
                '{"recommended_plan":{"summary":"数据库连接超时导致接口错误率升高",'
                '"actions":["检查连接池耗尽情况","查看慢查询和锁等待"],"risk_level":"medium"},'
                '"evidence":["RAG 命中数据库连接池排查文档"],"need_human":true}'
            )
        return "订单接口出现数据库连接超时，需要结合 RAG 和实时数据继续诊断。"


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


def test_interactive_topic_includes_llm_diagnosis_nodes() -> None:
    async def run() -> None:
        llm = WorkflowLLM()
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            llm=llm,  # type: ignore[arg-type]
        )
        state = {
            "query": "订单接口 timeout",
            "task_id": "topic-llm",
            "chat_id": "oc-chat",
            "root_message_id": "om-root",
            "node_results": [],
            "retry_counts": {},
        }

        understand_result, diagnosis_state = await workflow._understand(state)  # noqa: SLF001
        state["diagnosis_state"] = diagnosis_state
        plan_result, diagnosis_state = await workflow._generate_plan(state)  # noqa: SLF001
        state["diagnosis_state"] = diagnosis_state
        validate_result, diagnosis_state = await workflow._validate(state)  # noqa: SLF001

        step_names = [step.node_name for step in WORKFLOW_STEPS]
        assert "understand" in step_names
        assert "generate_plan" in step_names
        assert "validate" in step_names
        assert "告警理解完成" in understand_result
        assert "方案生成完成" in plan_result
        assert "方案校验完成：通过" in validate_result
        assert diagnosis_state["validation_result"] is True
        assert len(llm.prompts) == 3

    asyncio.run(run())


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


def test_interactive_topic_confirmation_replay_does_not_rerun_node() -> None:
    async def run() -> None:
        sender = FakeSender()
        workflow = InteractiveTopicWorkflow(sender=sender)  # type: ignore[arg-type]
        task = workflow.task_store.create_task("topic-replay", "oc-chat", "om-root", "数据库连接超时")
        workflow.task_store.set_card_message_id(task.task_id, "om-card")
        workflow.task_store.mark_waiting(
            task.task_id,
            "tool_call",
            "om-card",
            lambda _task_id, _node_name: None,
            "实时数据结果",
        )
        workflow.task_store.confirm_action(task.task_id, "tool_call", "next", source="test")

        called = False

        async def runner(_state):
            nonlocal called
            called = True
            raise AssertionError("runner should not be called during confirmation replay")

        result = await workflow._interactive_node(  # noqa: SLF001
            "tool_call",
            runner,
            {
                "task_id": task.task_id,
                "query": "数据库连接超时",
                "node_results": [],
                "retry_counts": {},
            },
        )

        assert called is False
        assert result["last_action"] == "next"
        assert result["node_result"] == "实时数据结果"
        assert result["node_results"] == [{"node_name": "tool_call", "result": "实时数据结果"}]
        assert sender.updates == []

    asyncio.run(run())


def test_interactive_topic_rag_uses_real_retriever() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=FakeRetriever(),  # type: ignore[arg-type]
        )

        result, diagnosis_state = await workflow._rag_retrieve({"query": "订单消息堆积"})  # noqa: SLF001

        assert "文档/混合召回 Top 1 / 共命中 1 条" in result
        assert "消息堆积排查" in result
        assert "section=第一章_消息队列_1.1_消息堆积" in result
        assert "真实检索片段" in result
        assert diagnosis_state["retrieved_docs"]

    asyncio.run(run())


def test_interactive_topic_cache_check_uses_history_case_store() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            case_store=FakeCaseStore(),  # type: ignore[arg-type]
        )

        result, diagnosis_state = await workflow._cache_check({"query": "CPU 飙升"})  # noqa: SLF001

        assert "命中 1 条可复用历史成功案例" in result
        assert "alert-case-1" in result
        assert "扩容 worker" in result
        assert diagnosis_state["cache_hit"] is True

    asyncio.run(run())


def test_interactive_topic_rag_displays_top2_only() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=MultiDocRetriever(),  # type: ignore[arg-type]
        )

        result, diagnosis_state = await workflow._rag_retrieve({"query": "CPU 飙升"})  # noqa: SLF001

        assert "文档/混合召回 Top 2 / 共命中 3 条" in result
        assert "doc-1" in result
        assert "doc-2" in result
        assert "doc-3" not in result
        assert len(diagnosis_state["retrieved_docs"]) == 3

    asyncio.run(run())


def test_interactive_topic_rag_also_shows_history_cases() -> None:
    async def run() -> None:
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            retriever=FakeRetriever(),  # type: ignore[arg-type]
            case_store=FakeCaseStore(),  # type: ignore[arg-type]
        )

        result, diagnosis_state = await workflow._rag_retrieve({"query": "CPU 飙升"})  # noqa: SLF001

        assert "文档/混合召回 Top 1 / 共命中 1 条" in result
        assert "历史案例召回 Top 1" in result
        assert "alert-case-1" in result
        assert "扩容 worker" in result
        assert len(diagnosis_state["retrieved_docs"]) == 2

    asyncio.run(run())


def test_interactive_topic_summary_uses_llm_context() -> None:
    async def run() -> None:
        llm = FakeLLM()
        workflow = InteractiveTopicWorkflow(
            sender=FakeSender(),  # type: ignore[arg-type]
            llm=llm,  # type: ignore[arg-type]
        )

        result = await workflow._summary(  # noqa: SLF001
            {
                "query": "数据库连接超时",
                "node_results": [
                    {"node_name": "rag_retrieve", "result": "RAG 命中数据库连接池排查文档"},
                    {"node_name": "tool_call", "result": "实时数据发现 timeout 错误率升高"},
                ],
            }
        )

        assert result == "LLM 诊断总结"
        assert "数据库连接超时" in llm.prompts[0]
        assert "rag_retrieve" in llm.prompts[0]
        assert "tool_call" in llm.prompts[0]
        assert "实时数据发现 timeout 错误率升高" in llm.prompts[0]

    asyncio.run(run())


def test_interactive_topic_summary_prompt_forbids_json_and_requires_markdown_sections() -> None:
    prompt = _build_summary_prompt(
        {
            "query": "数据库连接超时",
            "node_results": [
                {"node_name": "rag_retrieve", "result": "RAG 命中数据库连接池排查文档"},
                {"node_name": "tool_call", "result": "实时数据发现 timeout 错误率升高"},
            ],
        }
    )

    assert "不要输出 JSON" in prompt
    assert "不要输出代码块" in prompt
    assert "## 故障判断" in prompt
    assert "## 关键证据" in prompt
    assert "## 处置建议" in prompt
    assert "## 风险与观察项" in prompt
    assert "当前证据不足" in prompt


def test_interactive_topic_final_text_only_shows_summary_result() -> None:
    workflow = InteractiveTopicWorkflow(sender=FakeSender())  # type: ignore[arg-type]

    result = workflow._build_final_text(  # noqa: SLF001
        {
            "node_results": [
                {"node_name": "cache_check", "result": "cache details should be hidden"},
                {"node_name": "rag_retrieve", "result": "rag details should be hidden"},
                {"node_name": "tool_call", "result": "tool details should be hidden"},
                {"node_name": "summary", "result": "final diagnosis only"},
            ]
        }
    )

    assert result == "final diagnosis only"
    assert "rag details" not in result
    assert "tool details" not in result


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
