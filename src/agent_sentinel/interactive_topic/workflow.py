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
from agent_sentinel.interactive_topic.topic_sender import InteractiveTopicSender

logger = logging.getLogger(__name__)

NodeRunner = Callable[[TopicFlowState], Awaitable[str]]


class InteractiveTopicWorkflow:
    """Interactive Feishu topic workflow driven by LangGraph interrupts."""

    def __init__(
        self,
        *,
        sender: InteractiveTopicSender,
        wait_seconds: int = 5,
        checkpointer: Any | None = None,
    ) -> None:
        self.sender = sender
        self.wait_seconds = wait_seconds
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
        await self.sender.send_topic_text(
            chat_id,
            root_message_id,
            f"已创建 LangGraph 交互式话题，任务 ID：{task_id}",
        )
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
        if not task_id or not node_name or action not in {"next", "retry"}:
            return {"status": "ignored"}

        task = self.task_store.confirm_action(task_id, node_name, action, source=source)
        if task is None:
            return {"status": "ignored"}

        if action == "retry":
            await self.sender.update_topic_card_status(
                task.card_message_id,
                task_id,
                node_name,
                self._latest_node_result(task_id),
                "用户已选择重试",
            )
            await self.sender.send_topic_text(
                task.chat_id,
                task.root_message_id,
                f"用户拒绝结果，正在重试 {node_name} 节点...",
            )
        else:
            status_text = "超时自动继续" if source == "timeout" else "用户已确认，继续执行"
            await self.sender.update_topic_card_status(
                task.card_message_id,
                task_id,
                node_name,
                self._latest_node_result(task_id),
                status_text,
            )
            if source == "timeout":
                await self.sender.send_topic_text(task.chat_id, task.root_message_id, "超时未操作，自动继续")

        await self._drive(task_id, Command(resume={"action": action, "source": source}))
        return {"status": "ok"}

    async def _interactive_node(
        self,
        node_name: str,
        runner: NodeRunner,
        state: TopicFlowState,
    ) -> TopicFlowState:
        chat_id = state["chat_id"]
        root_message_id = state["root_message_id"]
        task_id = state["task_id"]
        await self.sender.send_topic_text(chat_id, root_message_id, f"正在执行 {node_name} 节点...")
        node_result = await runner(state)
        card_message_id = await self.sender.send_topic_card(
            chat_id,
            root_message_id,
            task_id,
            node_name,
            node_result,
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
                    await self.sender.send_topic_text(task.chat_id, task.root_message_id, final_text)
                self.task_store.finish_task(task_id)
                async with self._task_locks_guard:
                    self._task_locks.pop(task_id, None)

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
        return "retry" if state.get("last_action") == "retry" else "next"

    async def _cache_check(self, state: TopicFlowState) -> str:
        await asyncio.sleep(0.1)
        return f"缓存检查完成：未命中可复用诊断缓存，继续分析用户问题「{state.get('query', '')}」。"

    async def _rag_retrieve(self, state: TopicFlowState) -> str:
        await asyncio.sleep(0.1)
        return "RAG 检索完成：匹配到 3 条相似运维案例，包括连接池耗尽、消息堆积和慢查询放大。"

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

    def _latest_node_result(self, task_id: str) -> str:
        app = self.compile()
        try:
            snapshot = app.get_state({"configurable": {"thread_id": task_id}, "recursion_limit": 50})
            values = getattr(snapshot, "values", {}) or {}
            result = values.get("node_result")
            return str(result or "")
        except Exception:
            logger.debug("Failed to load latest node result task_id=%s", task_id, exc_info=True)
            return ""

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
