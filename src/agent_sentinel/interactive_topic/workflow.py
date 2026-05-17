from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from agent_sentinel.interactive_topic.state import TopicFlowState, append_node_result, increment_retry
from agent_sentinel.interactive_topic.task_store import TopicTaskStore
from agent_sentinel.interactive_topic.topic_sender import InteractiveTopicSender, WORKFLOW_STEPS
from agent_sentinel.rag.base import BaseRetriever
from agent_sentinel.rag.history_cases import HistoryCaseStore

logger = logging.getLogger(__name__)

NodeRunner = Callable[[TopicFlowState], Awaitable[str]]
MAX_NODE_RETRIES = 2
RAG_DISPLAY_TOP_K = 2


class InteractiveTopicWorkflow:
    """Interactive Feishu workflow using one continuously updated card."""

    def __init__(
        self,
        *,
        sender: InteractiveTopicSender,
        wait_seconds: int = 5,
        retriever: BaseRetriever | None = None,
        case_store: HistoryCaseStore | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.sender = sender
        self.wait_seconds = wait_seconds
        self.retriever = retriever
        self.case_store = case_store
        self.task_store = TopicTaskStore(wait_seconds)
        self.checkpointer = checkpointer or InMemorySaver()
        self._compiled: Any | None = None
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._task_locks_guard = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_started = threading.Event()
        self._loop_guard = threading.Lock()

    def compile(self) -> Any:
        if self._compiled is not None:
            return self._compiled

        builder = StateGraph(TopicFlowState)
        builder.add_node("cache_check", partial(self._interactive_node, "cache_check", self._cache_check))
        builder.add_node("rag_retrieve", partial(self._interactive_node, "rag_retrieve", self._rag_retrieve))
        builder.add_node("tool_call", partial(self._interactive_node, "tool_call", self._tool_call))
        builder.add_node("summary", partial(self._interactive_node, "summary", self._summary))

        builder.set_entry_point("cache_check")
        builder.add_conditional_edges(
            "cache_check",
            self._route_after_confirm,
            {"retry": "cache_check", "next": "rag_retrieve"},
        )
        builder.add_conditional_edges(
            "rag_retrieve",
            self._route_after_confirm,
            {"retry": "rag_retrieve", "next": "tool_call"},
        )
        builder.add_conditional_edges(
            "tool_call",
            self._route_after_confirm,
            {"retry": "tool_call", "next": "summary"},
        )
        builder.add_conditional_edges(
            "summary",
            self._route_after_confirm,
            {"retry": "summary", "next": END},
        )
        self._compiled = builder.compile(checkpointer=self.checkpointer)
        logger.info("Interactive topic LangGraph compiled")
        return self._compiled

    async def start(self, chat_id: str, root_message_id: str, query: str) -> str:
        future = self._submit(self._start_impl(chat_id, root_message_id, query))
        return await asyncio.wrap_future(future)

    def start_sync(self, chat_id: str, root_message_id: str, query: str) -> tuple[str, bool]:
        future = self._submit(self._start_impl(chat_id, root_message_id, query))
        task_id = future.result()
        return f"Interactive topic workflow started: {task_id}", True

    async def handle_card_callback(self, payload: dict[str, Any], *, source: str = "card") -> dict[str, str]:
        future = self._submit(self._handle_card_callback_impl(payload, source=source))
        return await asyncio.wrap_future(future)

    def handle_card_callback_sync(self, payload: dict[str, Any], *, source: str = "card") -> dict[str, str]:
        future = self._submit(self._handle_card_callback_impl(payload, source=source))
        return future.result()

    async def _start_impl(self, chat_id: str, root_message_id: str, query: str) -> str:
        task_id = f"topic-{uuid.uuid4()}"
        self.task_store.create_task(task_id, chat_id, root_message_id, query)
        card_message_id = await self.sender.send_workflow_card(chat_id, root_message_id, task_id, query)
        self.task_store.set_card_message_id(task_id, card_message_id)
        initial_state: TopicFlowState = {
            "query": query,
            "task_id": task_id,
            "chat_id": chat_id,
            "root_message_id": root_message_id,
            "retry_counts": {},
            "node_results": [],
        }
        await self._drive(task_id, initial_state)
        return task_id

    async def _handle_card_callback_impl(self, payload: dict[str, Any], *, source: str = "card") -> dict[str, str]:
        value = self._extract_card_value(payload)
        task_id = str(value.get("task_id") or "")
        node_name = str(value.get("node_name") or value.get("node") or "")
        action = str(value.get("action") or value.get("operate") or "")
        if action == "noop":
            return {"status": "ignored"}
        if action in {"feedback_valid", "feedback_invalid"}:
            return await self._handle_feedback_callback(task_id, action, source=source)
        if not task_id or not node_name or action not in {"next", "retry"}:
            return {"status": "ignored"}

        task = self.task_store.confirm_action(task_id, node_name, action, source=source)
        if task is None:
            return {"status": "ignored"}

        if action == "retry":
            await self.sender.update_workflow_card(
                task.card_message_id,
                task_id=task_id,
                query=task.query,
                node_statuses=self._node_statuses_until(node_name, "failed", self._node_results(task_id)),
                current_node=node_name,
                current_result="用户已拒绝当前节点结果，准备重试该节点。",
                buttons_node=None,
            )
        else:
            status_text = "超时自动继续" if source == "timeout" else "用户已同意，继续执行"
            await self.sender.update_workflow_card(
                task.card_message_id,
                task_id=task_id,
                query=task.query,
                node_statuses=self._node_statuses_until(node_name, "done", self._node_results(task_id)),
                current_node=node_name,
                current_result=status_text,
                buttons_node=None,
            )

        await self._drive(task_id, Command(resume={"action": action, "source": source}))
        return {"status": "ok"}

    async def _interactive_node(
        self,
        node_name: str,
        runner: NodeRunner,
        state: TopicFlowState,
    ) -> TopicFlowState:
        task_id = state["task_id"]
        task = self.task_store.get_task(task_id)
        card_message_id = task.card_message_id if task else None
        query = state.get("query", "")

        await self.sender.update_workflow_card(
            card_message_id,
            task_id=task_id,
            query=query,
            node_statuses=self._node_statuses_until(node_name, "running", state.get("node_results", [])),
            current_node=node_name,
            current_result=f"正在执行 {_node_title(node_name)} 节点...",
            buttons_node=None,
        )

        try:
            node_result = await runner(state)
        except Exception as exc:
            logger.exception("Interactive topic node failed task_id=%s node=%s", task_id, node_name)
            node_result = f"{_node_title(node_name)} 节点执行失败，已跳过当前节点并继续后续流程：{exc}"
            await self.sender.update_workflow_card(
                card_message_id,
                task_id=task_id,
                query=query,
                node_statuses=self._node_statuses_until(node_name, "skipped", state.get("node_results", [])),
                current_node=node_name,
                current_result=node_result,
                buttons_node=None,
            )
            return {
                "current_node": node_name,
                "node_result": node_result,
                "last_action": "skip",
                "retry_counts": dict(state.get("retry_counts", {})),
                "node_results": append_node_result(state, node_name, node_result),
            }

        await self.sender.update_workflow_card(
            card_message_id,
            task_id=task_id,
            query=query,
            node_statuses=self._node_statuses_until(node_name, "waiting", state.get("node_results", [])),
            current_node=node_name,
            current_result=node_result,
            buttons_node=node_name,
        )

        self.task_store.mark_waiting(
            task_id,
            node_name,
            card_message_id,
            lambda timeout_task_id, timeout_node_name: self._on_timeout(timeout_task_id, timeout_node_name),
        )

        resume_payload = interrupt(
            {
                "task_id": task_id,
                "node_name": node_name,
                "card_message_id": card_message_id,
                "timeout_seconds": self.wait_seconds,
            }
        )
        action = str((resume_payload or {}).get("action") or "next")
        if action not in {"next", "retry"}:
            action = "next"
        retry_counts = increment_retry(state, node_name) if action == "retry" else dict(state.get("retry_counts", {}))
        if action == "retry" and retry_counts.get(node_name, 0) >= MAX_NODE_RETRIES:
            action = "skip"
            node_result = f"{node_result}\n\n已重试 {MAX_NODE_RETRIES} 次，自动跳过当前节点并进入下一节点。"
            await self.sender.update_workflow_card(
                card_message_id,
                task_id=task_id,
                query=query,
                node_statuses=self._node_statuses_until(node_name, "skipped", state.get("node_results", [])),
                current_node=node_name,
                current_result=node_result,
                buttons_node=None,
            )
        return {
            "current_node": node_name,
            "node_result": node_result,
            "last_action": action,
            "retry_counts": retry_counts,
            "node_results": append_node_result(state, node_name, node_result),
        }

    async def _drive(self, task_id: str, graph_input: TopicFlowState | Command) -> None:
        app = self.compile()
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 50}
        lock = await self._get_task_lock(task_id)
        async with lock:
            async for update in app.astream(graph_input, config=config, stream_mode="updates"):
                logger.debug("Interactive topic workflow update task_id=%s update=%s", task_id, update)
            snapshot = await app.aget_state(config)
            next_nodes = tuple(getattr(snapshot, "next", ()) or ())
            values = getattr(snapshot, "values", {}) or {}
            if not next_nodes:
                task = self.task_store.get_task(task_id)
                if task is not None:
                    final_text = self._build_final_text(values)
                    await self.sender.update_workflow_card(
                        task.card_message_id,
                        task_id=task_id,
                        query=task.query,
                        node_statuses={step.node_name: "done" for step in WORKFLOW_STEPS},
                        current_node=None,
                        current_result=final_text,
                        buttons_node=None,
                        feedback_buttons=True,
                    )
                    self.task_store.mark_feedback_waiting(task_id, task.card_message_id)

    def _on_timeout(self, task_id: str, node_name: str) -> None:
        try:
            logger.info("Interactive topic timeout fired task_id=%s node=%s", task_id, node_name)
            self.handle_card_callback_sync(
                {"value": {"task_id": task_id, "node_name": node_name, "action": "next"}},
                source="timeout",
            )
        except Exception:
            logger.exception("Interactive topic timeout handling failed task_id=%s node=%s", task_id, node_name)

    async def _get_task_lock(self, task_id: str) -> asyncio.Lock:
        async with self._task_locks_guard:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._task_locks[task_id] = lock
            return lock

    def _route_after_confirm(self, state: TopicFlowState) -> str:
        if state.get("last_action") != "retry":
            return "next"
        current_node = str(state.get("current_node") or "")
        retry_count = state.get("retry_counts", {}).get(current_node, 0)
        return "retry" if retry_count < MAX_NODE_RETRIES else "next"

    async def _cache_check(self, state: TopicFlowState) -> str:
        query = state.get("query", "")
        if self.case_store is None:
            return "缓存检查跳过：历史案例库未配置，请检查 RAG_PROVIDER 和 MILVUS_URI。"
        docs = await self.case_store.search_similar_cases(query, top_k=RAG_DISPLAY_TOP_K)
        if not docs:
            return f"缓存检查完成：未命中可复用诊断缓存，继续分析用户问题「{query}」。"

        lines = [f"缓存检查完成：命中 {len(docs)} 条可复用历史成功案例。"]
        for index, doc in enumerate(docs, start=1):
            metadata = doc.metadata or {}
            plan = metadata.get("recommended_plan") or metadata.get("final_text") or ""
            plan_text = str(plan).replace("\n", " ")[:220] or "-"
            lines.append(
                f"{index}. {doc.title or doc.id} | score={doc.score:.4f}\n"
                f"   case_id={doc.id}\n"
                f"   历史方案={plan_text}"
            )
        return "\n".join(lines)

    async def _rag_retrieve(self, state: TopicFlowState) -> str:
        query = state.get("query", "")
        docs = await self.retriever.retrieve(query) if self.retriever is not None else []
        history_docs = await self.case_store.search_similar_cases(query, top_k=RAG_DISPLAY_TOP_K) if self.case_store else []

        if not docs and not history_docs:
            return "RAG 检索完成：真实向量库未命中相关运维片段或历史案例。"

        lines = ["RAG 检索完成："]
        if docs:
            display_docs = docs[:RAG_DISPLAY_TOP_K]
            lines.append(f"\n**文档/混合召回 Top {len(display_docs)} / 共命中 {len(docs)} 条**")
            for index, doc in enumerate(display_docs, start=1):
                metadata = doc.metadata or {}
                section = metadata.get("section") or metadata.get("h2") or metadata.get("h1") or "-"
                category = metadata.get("alert_category") or "-"
                severity = metadata.get("severity_level") or "-"
                source = doc.source_uri or metadata.get("source") or "-"
                preview = doc.text.replace("\n", " ")[:160]
                lines.append(
                    f"{index}. {doc.title or doc.id} | score={doc.score:.4f} | category={category} | "
                    f"severity={severity}\n   section={section}\n   source={source}\n   {preview}"
                )
        else:
            lines.append("\n**文档/混合召回**：未命中")

        if history_docs:
            lines.append(f"\n**历史案例召回 Top {len(history_docs)}**")
            for index, doc in enumerate(history_docs, start=1):
                metadata = doc.metadata or {}
                plan = metadata.get("recommended_plan") or metadata.get("final_text") or ""
                plan_text = str(plan).replace("\n", " ")[:220] or "-"
                lines.append(
                    f"{index}. {doc.title or doc.id} | score={doc.score:.4f}\n"
                    f"   case_id={doc.id}\n"
                    f"   历史方案={plan_text}"
                )
        else:
            lines.append("\n**历史案例召回**：未命中")
        return "\n".join(lines)

    async def _handle_feedback_callback(self, task_id: str, action: str, *, source: str) -> dict[str, str]:
        if not task_id:
            return {"status": "ignored"}
        task = self.task_store.confirm_feedback(task_id, action, source=source)
        if task is None:
            return {"status": "ignored"}

        app = self.compile()
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 50}
        snapshot = await app.aget_state(config)
        values = getattr(snapshot, "values", {}) or {}
        final_text = self._build_final_text(values)
        saved_case_id = ""
        if action == "feedback_valid":
            if self.case_store is not None:
                try:
                    saved_case_id = await self.case_store.save_case_to_history(
                        self._build_feedback_state(task, values, final_text)
                    )
                except Exception as exc:
                    logger.exception("Interactive topic feedback save failed task_id=%s", task_id)
                    self.task_store.reset_feedback_waiting(task_id)
                    await self.sender.update_workflow_card(
                        task.card_message_id,
                        task_id=task_id,
                        query=task.query,
                        node_statuses={step.node_name: "done" for step in WORKFLOW_STEPS},
                        current_node=None,
                        current_result=f"{final_text}\n\n❌ 入库失败：{exc}\n请修复后重试，或选择不存储。",
                        buttons_node=None,
                        feedback_buttons=True,
                    )
                    return {"status": "error", "reason": "save_failed"}
                result = f"{final_text}\n\n✅ 已存入历史案例知识库：{saved_case_id}"
            else:
                result = f"{final_text}\n\n✅ 已确认有效；历史案例库未配置，未执行入库。"
        else:
            result = f"{final_text}\n\n❌ 已标记为无效，本次诊断结果不入库。"

        await self.sender.update_workflow_card(
            task.card_message_id,
            task_id=task_id,
            query=task.query,
            node_statuses={step.node_name: "done" for step in WORKFLOW_STEPS},
            current_node=None,
            current_result=result,
            buttons_node=None,
            feedback_buttons=False,
        )
        self.task_store.finish_task(task_id)
        async with self._task_locks_guard:
            self._task_locks.pop(task_id, None)
        return {"status": "ok", "saved_case_id": saved_case_id}

    async def _tool_call(self, state: TopicFlowState) -> str:
        await asyncio.sleep(0.1)
        return "工具调用完成：指标显示错误率升高，日志出现 timeout，拓扑显示 database -> api 链路异常。"

    async def _summary(self, state: TopicFlowState) -> str:
        await asyncio.sleep(0.1)
        return "总结完成：建议先限流止血，扩容消费者，检查数据库慢查询，并持续观察错误率回落。"

    def _build_final_text(self, state: dict[str, Any]) -> str:
        results = state.get("node_results", [])
        lines = ["LangGraph 交互式话题流程执行完毕。"]
        for item in results:
            if isinstance(item, dict):
                lines.append(f"- {item.get('node_name')}: {item.get('result')}")
        return "\n".join(lines)

    def _build_feedback_state(self, task: Any, values: dict[str, Any], final_text: str) -> dict[str, Any]:
        node_results = values.get("node_results", [])
        result_by_node = {
            str(item.get("node_name")): str(item.get("result") or "")
            for item in node_results
            if isinstance(item, dict)
        }
        evidence = [str(item.get("result")) for item in node_results if isinstance(item, dict) and item.get("result")]
        return {
            "raw_alert": {
                "source": "interactive-topic",
                "summary": task.query,
                "tags": ["interactive-topic"],
            },
            "alert_summary": task.query,
            "retrieved_docs": [result_by_node.get("rag_retrieve", "")],
            "live_data": {"tool_call": result_by_node.get("tool_call", "")},
            "recommended_plan": {"summary": result_by_node.get("summary") or final_text},
            "evidence": evidence,
            "validation_result": True,
            "need_human": False,
            "human_decision": "feedback_valid",
            "final_text": final_text,
            "messages": [{"role": "assistant", "content": final_text}],
        }

    def _node_results(self, task_id: str) -> list[dict[str, str]]:
        app = self.compile()
        try:
            snapshot = app.get_state({"configurable": {"thread_id": task_id}, "recursion_limit": 50})
            values = getattr(snapshot, "values", {}) or {}
            results = values.get("node_results")
            return results if isinstance(results, list) else []
        except Exception:
            logger.debug("Failed to load node results task_id=%s", task_id, exc_info=True)
            return []

    def _node_statuses_until(
        self,
        current_node: str,
        current_status: str,
        node_results: list[dict[str, str]],
    ) -> dict[str, str]:
        completed = {
            str(item.get("node_name"))
            for item in node_results
            if isinstance(item, dict) and item.get("node_name")
        }
        statuses: dict[str, str] = {}
        for step in WORKFLOW_STEPS:
            if step.node_name in completed:
                statuses[step.node_name] = "done"
        statuses[current_node] = current_status
        return statuses

    def _extract_card_value(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if isinstance(action, dict):
            value = action.get("value")
            if isinstance(value, dict):
                return value
        event = payload.get("event")
        if isinstance(event, dict):
            action = event.get("action")
            if isinstance(action, dict):
                value = action.get("value")
                if isinstance(value, dict):
                    return value
        value = payload.get("value")
        if isinstance(value, dict):
            return value
        return payload

    def _submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        loop = self._ensure_background_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _ensure_background_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_guard:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop_started.clear()
            self._loop_thread = threading.Thread(
                target=self._run_background_loop,
                name="interactive-topic-loop",
                daemon=True,
            )
            self._loop_thread.start()
        self._loop_started.wait(timeout=5)
        if self._loop is None:
            raise RuntimeError("Interactive topic background loop failed to start.")
        return self._loop

    def _run_background_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_started.set()
        logger.info("Interactive topic background event loop started")
        loop.run_forever()


def _node_title(node_name: str) -> str:
    for step in WORKFLOW_STEPS:
        if step.node_name == node_name:
            return step.title
    return node_name
