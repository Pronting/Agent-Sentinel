from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from agent_sentinel.agents.fetch_tools import fetch_live_data_node
from agent_sentinel.agents.generate_plan import generate_plan_node
from agent_sentinel.agents.understand_agent import understand_node
from agent_sentinel.agents.validate_plan import validate_plan_node
from agent_sentinel.graph.state import DiagnosisState
from agent_sentinel.interactive_topic.state import TopicFlowState, append_node_result, increment_retry
from agent_sentinel.interactive_topic.task_store import TopicTaskStore
from agent_sentinel.interactive_topic.topic_sender import InteractiveTopicSender, WORKFLOW_STEPS
from agent_sentinel.llm.executor import LLMExecutor
from agent_sentinel.monitoring import monitor, trace_id_from_parts
from agent_sentinel.rag.base import BaseRetriever
from agent_sentinel.rag.history_cases import HistoryCaseStore

logger = logging.getLogger(__name__)

NodeRunResult = str | tuple[str, DiagnosisState]
NodeRunner = Callable[[TopicFlowState], Awaitable[NodeRunResult]]
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
        llm: LLMExecutor | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.sender = sender
        self.wait_seconds = wait_seconds
        self.retriever = retriever
        self.case_store = case_store
        self.llm = llm
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
        builder.add_node("understand", partial(self._interactive_node, "understand", self._understand))
        builder.add_node("cache_check", partial(self._interactive_node, "cache_check", self._cache_check))
        builder.add_node("rag_retrieve", partial(self._interactive_node, "rag_retrieve", self._rag_retrieve))
        builder.add_node("tool_call", partial(self._interactive_node, "tool_call", self._tool_call))
        builder.add_node("generate_plan", partial(self._interactive_node, "generate_plan", self._generate_plan))
        builder.add_node("validate", partial(self._interactive_node, "validate", self._validate))
        builder.add_node("summary", partial(self._interactive_node, "summary", self._summary))

        builder.set_entry_point("understand")
        builder.add_conditional_edges(
            "understand",
            self._route_after_confirm,
            {"retry": "understand", "next": "cache_check"},
        )
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
            {"retry": "tool_call", "next": "generate_plan"},
        )
        builder.add_conditional_edges(
            "generate_plan",
            self._route_after_confirm,
            {"retry": "generate_plan", "next": "validate"},
        )
        builder.add_conditional_edges(
            "validate",
            self._route_after_confirm,
            {"retry": "validate", "next": "summary"},
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
        started = time.perf_counter()
        task_id = f"topic-{uuid.uuid4()}"
        logger.info(
            "Interactive topic start trace_id=%s task_id=%s chat_id=%s root_message_id=%s query_chars=%s wait_seconds=%s",
            trace_id_from_parts(chat_id, root_message_id),
            task_id,
            chat_id,
            root_message_id,
            len(query or ""),
            self.wait_seconds,
        )
        self.task_store.create_task(task_id, chat_id, root_message_id, query)
        if monitor.enabled:
            monitor.current_workflow_active.labels(workflow_type="interactive_topic", group_id=chat_id or "unknown").inc()
        card_message_id = await self.sender.send_workflow_card(chat_id, root_message_id, task_id, query)
        logger.info("Interactive topic initial card sent task_id=%s card_message_id=%s", task_id, card_message_id)
        self.task_store.set_card_message_id(task_id, card_message_id)
        initial_state: TopicFlowState = {
            "query": query,
            "task_id": task_id,
            "chat_id": chat_id,
            "root_message_id": root_message_id,
            "trace_id": trace_id_from_parts(chat_id, root_message_id),
            "retry_counts": {},
            "node_results": [],
            "diagnosis_state": self._initial_diagnosis_state(
                chat_id=chat_id,
                root_message_id=root_message_id,
                query=query,
                task_id=task_id,
            ),
        }
        await self._drive(task_id, initial_state)
        logger.info("Interactive topic start submitted task_id=%s elapsed_ms=%s", task_id, int((time.perf_counter() - started) * 1000))
        return task_id

    async def _handle_card_callback_impl(self, payload: dict[str, Any], *, source: str = "card") -> dict[str, str]:
        value = self._extract_card_value(payload)
        task_id = str(value.get("task_id") or "")
        node_name = str(value.get("node_name") or value.get("node") or "")
        action = str(value.get("action") or value.get("operate") or "")
        logger.info(
            "Interactive topic card callback parsed task_id=%s node=%s action=%s source=%s value_keys=%s",
            task_id,
            node_name,
            action,
            source,
            sorted(value.keys()),
        )
        if action == "noop":
            logger.info("Interactive topic callback ignored noop task_id=%s source=%s", task_id, source)
            return {"status": "ignored"}
        if action in {"feedback_valid", "feedback_invalid"}:
            return await self._handle_feedback_callback(task_id, action, source=source)
        if not task_id or not node_name or action not in {"next", "retry"}:
            logger.info("Interactive topic callback ignored invalid value task_id=%s node=%s action=%s source=%s", task_id, node_name, action, source)
            return {"status": "ignored"}

        task = self.task_store.confirm_action(task_id, node_name, action, source=source)
        if task is None:
            logger.info("Interactive topic callback ignored stale task_id=%s node=%s action=%s source=%s", task_id, node_name, action, source)
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
        logger.info("Interactive topic callback applied task_id=%s node=%s action=%s source=%s", task_id, node_name, action, source)
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
        started = time.perf_counter()
        replay_result = self._confirmed_replay_result(task, node_name)
        if replay_result is not None:
            action = str(task.last_action or "next") if task else "next"
            if action not in {"next", "retry"}:
                action = "next"
            retry_counts = increment_retry(state, node_name) if action == "retry" else dict(state.get("retry_counts", {}))
            logger.info(
                "Interactive node confirmation replay task_id=%s node=%s action=%s source_state=%s retry_count=%s",
                task_id,
                node_name,
                action,
                task.status if task else None,
                retry_counts.get(node_name, 0),
            )
            if action == "retry" and retry_counts.get(node_name, 0) >= MAX_NODE_RETRIES:
                action = "skip"
                replay_result = f"{replay_result}\n\n已重试 {MAX_NODE_RETRIES} 次，自动跳过当前节点并进入下一节点。"
                await self.sender.update_workflow_card(
                    card_message_id,
                    task_id=task_id,
                    query=query,
                    node_statuses=self._node_statuses_until(node_name, "skipped", state.get("node_results", [])),
                    current_node=node_name,
                    current_result=replay_result,
                    buttons_node=None,
                )
                logger.info("Interactive node retry limit reached task_id=%s node=%s max_retries=%s", task_id, node_name, MAX_NODE_RETRIES)
            replay_update: TopicFlowState = {
                "current_node": node_name,
                "node_result": replay_result,
                "last_action": action,
                "retry_counts": retry_counts,
                "node_results": append_node_result(state, node_name, replay_result),
            }
            if action == "next" and task and task.pending_diagnosis_state:
                replay_update["diagnosis_state"] = task.pending_diagnosis_state  # type: ignore[typeddict-item]
            return replay_update

        logger.info(
            "Interactive node start trace_id=%s task_id=%s node=%s retry_count=%s previous_results=%s card_message_id=%s",
            state.get("trace_id") or trace_id_from_parts(state.get("chat_id"), state.get("root_message_id")),
            task_id,
            node_name,
            state.get("retry_counts", {}).get(node_name, 0),
            len(state.get("node_results", [])),
            card_message_id,
        )

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
            with monitor.track_node(node_name, state.get("chat_id")):
                run_result = await runner(state)
                node_result, diagnosis_state = self._normalize_node_result(run_result, state)
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

        logger.info(
            "Interactive node completed task_id=%s node=%s result_chars=%s elapsed_ms=%s",
            task_id,
            node_name,
            len(node_result or ""),
            int((time.perf_counter() - started) * 1000),
        )
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
            node_result,
            diagnosis_state,
        )
        logger.info(
            "Interactive node waiting for confirmation task_id=%s node=%s timeout_seconds=%s",
            task_id,
            node_name,
            self.wait_seconds,
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
        logger.info(
            "Interactive node resumed task_id=%s node=%s action=%s source=%s retry_count=%s",
            task_id,
            node_name,
            action,
            (resume_payload or {}).get("source"),
            retry_counts.get(node_name, 0),
        )
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
            logger.info("Interactive node retry limit reached task_id=%s node=%s max_retries=%s", task_id, node_name, MAX_NODE_RETRIES)
        update: TopicFlowState = {
            "current_node": node_name,
            "node_result": node_result,
            "last_action": action,
            "retry_counts": retry_counts,
            "node_results": append_node_result(state, node_name, node_result),
        }
        if action == "next":
            update["diagnosis_state"] = diagnosis_state
        return update

    async def _drive(self, task_id: str, graph_input: TopicFlowState | Command) -> None:
        app = self.compile()
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 50}
        lock = await self._get_task_lock(task_id)
        started = time.perf_counter()
        logger.info("Interactive drive start task_id=%s input_type=%s", task_id, type(graph_input).__name__)
        async with lock:
            async for update in app.astream(graph_input, config=config, stream_mode="updates"):
                logger.info("Interactive drive update task_id=%s nodes=%s", task_id, list(update.keys()))
            snapshot = await app.aget_state(config)
            next_nodes = tuple(getattr(snapshot, "next", ()) or ())
            values = getattr(snapshot, "values", {}) or {}
            logger.info(
                "Interactive drive snapshot task_id=%s next_nodes=%s node_results=%s elapsed_ms=%s",
                task_id,
                next_nodes,
                len(values.get("node_results", []) or []),
                int((time.perf_counter() - started) * 1000),
            )
            if not next_nodes:
                task = self.task_store.get_task(task_id)
                if task is not None:
                    final_text = self._build_final_text(values)
                    if monitor.enabled and task.created_at_monotonic:
                        monitor.workflow_total_duration_seconds.labels(
                            workflow_type="interactive_topic",
                            group_id=task.chat_id or "unknown",
                        ).observe(time.perf_counter() - task.created_at_monotonic)
                    logger.info(
                        "Interactive topic final card update task_id=%s final_chars=%s node_results=%s",
                        task_id,
                        len(final_text),
                        len(values.get("node_results", []) or []),
                    )
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
                    if monitor.enabled:
                        monitor.current_workflow_active.labels(
                            workflow_type="interactive_topic",
                            group_id=task.chat_id or "unknown",
                        ).dec()
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

    def _initial_diagnosis_state(self, *, chat_id: str, root_message_id: str, query: str, task_id: str) -> DiagnosisState:
        trace_id = trace_id_from_parts(chat_id, root_message_id)
        return {
            "raw_alert": {
                "source": "feishu-user",
                "level": "INFO",
                "summary": query,
                "details": query,
                "raw_text": query,
                "trigger_type": "user_message",
                "tags": ["feishu", "interactive-topic"],
            },
            "chat_id": chat_id,
            "thread_root_message_id": root_message_id,
            "workflow_thread_id": task_id,
            "workflow_run_id": task_id,
            "trace_id": trace_id,
            "messages": [],
            "evidence": [],
        }

    def _diagnosis_state(self, state: TopicFlowState) -> DiagnosisState:
        diagnosis_state = state.get("diagnosis_state")
        if diagnosis_state:
            return diagnosis_state
        return self._initial_diagnosis_state(
            chat_id=state.get("chat_id", ""),
            root_message_id=state.get("root_message_id", ""),
            query=state.get("query", ""),
            task_id=state.get("task_id", ""),
        )

    def _merge_diagnosis_state(self, state: TopicFlowState, update: DiagnosisState) -> DiagnosisState:
        return {**self._diagnosis_state(state), **update}

    def _normalize_node_result(self, result: NodeRunResult, state: TopicFlowState) -> tuple[str, DiagnosisState]:
        if isinstance(result, tuple):
            return result
        return result, self._diagnosis_state(state)

    async def _understand(self, state: TopicFlowState) -> NodeRunResult:
        if self.llm is None:
            diagnosis_state = self._merge_diagnosis_state(state, {"alert_summary": state.get("query", "")})
            return f"告警理解完成：{state.get('query', '')}", diagnosis_state
        update = await understand_node(
            self._diagnosis_state(state),
            llm=self.llm,
            case_store=None,
            cache_enabled=False,
        )
        diagnosis_state = self._merge_diagnosis_state(state, update)
        summary = str(diagnosis_state.get("alert_summary") or state.get("query", ""))
        return f"告警理解完成：\n{summary}", diagnosis_state

    async def _cache_check(self, state: TopicFlowState) -> NodeRunResult:
        query = str(self._diagnosis_state(state).get("alert_summary") or state.get("query", ""))
        if self.case_store is None:
            logger.info("Interactive cache check skipped task_id=%s reason=no_case_store", state.get("task_id"))
            monitor.record_cache_miss(state.get("chat_id"))
            return "缓存检查跳过：历史案例库未配置，请检查 RAG_PROVIDER 和 MILVUS_URI。", self._diagnosis_state(state)
        docs = await self.case_store.search_similar_cases(query, top_k=RAG_DISPLAY_TOP_K)
        logger.info(
            "Interactive cache check completed task_id=%s hits=%s top_scores=%s",
            state.get("task_id"),
            len(docs),
            _format_scores([doc.score for doc in docs]),
        )
        if not docs:
            monitor.record_cache_miss(state.get("chat_id"))
            diagnosis_state = self._merge_diagnosis_state(state, {"cache_candidates": [], "cache_hit": False})
            return f"缓存检查完成：未命中可复用诊断缓存，继续分析用户问题「{query}」。", diagnosis_state

        monitor.record_cache_hit(state.get("chat_id"))
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
        diagnosis_state = self._merge_diagnosis_state(
            state,
            {
                "cache_candidates": [
                    {"id": doc.id, "title": doc.title, "score": doc.score, "metadata": doc.metadata}
                    for doc in docs
                ],
                "cache_hit": True,
            },
        )
        return "\n".join(lines), diagnosis_state

    async def _rag_retrieve(self, state: TopicFlowState) -> NodeRunResult:
        query = str(self._diagnosis_state(state).get("alert_summary") or state.get("query", ""))
        started = time.perf_counter()
        logger.info("Interactive RAG retrieve start task_id=%s query_chars=%s", state.get("task_id"), len(query))
        monitor.record_rag_retrieval("interactive_hybrid", state.get("chat_id"))
        docs = await self.retriever.retrieve(query) if self.retriever is not None else []
        history_docs = await self.case_store.search_similar_cases(query, top_k=RAG_DISPLAY_TOP_K) if self.case_store else []
        logger.info(
            "Interactive RAG retrieve completed task_id=%s docs=%s history_docs=%s doc_scores=%s history_scores=%s elapsed_ms=%s",
            state.get("task_id"),
            len(docs),
            len(history_docs),
            _format_scores([doc.score for doc in docs]),
            _format_scores([doc.score for doc in history_docs]),
            int((time.perf_counter() - started) * 1000),
        )

        retrieved_docs = [doc.to_prompt_text() for doc in docs]
        retrieved_docs.extend(doc.to_prompt_text() for doc in history_docs)
        diagnosis_state = self._merge_diagnosis_state(state, {"retrieved_docs": retrieved_docs})

        if not docs and not history_docs:
            return "RAG 检索完成：真实向量库未命中相关运维片段或历史案例。", diagnosis_state

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
        return "\n".join(lines), diagnosis_state

    async def _handle_feedback_callback(self, task_id: str, action: str, *, source: str) -> dict[str, str]:
        if not task_id:
            logger.info("Interactive feedback ignored missing task_id action=%s source=%s", action, source)
            return {"status": "ignored"}
        task = self.task_store.confirm_feedback(task_id, action, source=source)
        if task is None:
            logger.info("Interactive feedback ignored stale task_id=%s action=%s source=%s", task_id, action, source)
            return {"status": "ignored"}

        started = time.perf_counter()
        logger.info("Interactive feedback handling start task_id=%s action=%s source=%s", task_id, action, source)
        app = self.compile()
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 50}
        snapshot = await app.aget_state(config)
        values = getattr(snapshot, "values", {}) or {}
        final_text = self._build_final_text(values)
        saved_case_id = ""
        if action == "feedback_valid":
            monitor.record_feedback(True, task.chat_id)
            if self.case_store is not None:
                try:
                    saved_case_id = await self.case_store.save_case_to_history(
                        self._build_feedback_state(task, values, final_text)
                    )
                    logger.info("Interactive feedback saved case task_id=%s case_id=%s", task_id, saved_case_id)
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
                logger.info("Interactive feedback valid but case_store missing task_id=%s", task_id)
                result = f"{final_text}\n\n✅ 已确认有效；历史案例库未配置，未执行入库。"
        else:
            logger.info("Interactive feedback marked invalid task_id=%s", task_id)
            monitor.record_feedback(False, task.chat_id)
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
        logger.info(
            "Interactive feedback handling completed task_id=%s action=%s saved_case_id=%s elapsed_ms=%s",
            task_id,
            action,
            saved_case_id,
            int((time.perf_counter() - started) * 1000),
        )
        return {"status": "ok", "saved_case_id": saved_case_id}

    async def _tool_call(self, state: TopicFlowState) -> NodeRunResult:
        update = await fetch_live_data_node(self._diagnosis_state(state))
        diagnosis_state = self._merge_diagnosis_state(state, update)
        live_data = diagnosis_state.get("live_data", {})
        return f"工具调用完成：\n{_format_live_data(live_data)}", diagnosis_state

    async def _generate_plan(self, state: TopicFlowState) -> NodeRunResult:
        if self.llm is None:
            diagnosis_state = self._merge_diagnosis_state(
                state,
                {
                    "recommended_plan": {
                        "summary": "LLM 未配置，无法生成真实诊断方案。",
                        "actions": ["检查 LLM 配置后重试。"],
                        "risk_level": "unknown",
                    },
                    "need_human": True,
                },
            )
            return "方案生成跳过：LLM 未配置。", diagnosis_state
        update = await generate_plan_node(self._diagnosis_state(state), self.llm)
        diagnosis_state = self._merge_diagnosis_state(state, update)
        return _format_plan_result(diagnosis_state.get("recommended_plan", {}), diagnosis_state.get("evidence", [])), diagnosis_state

    async def _validate(self, state: TopicFlowState) -> NodeRunResult:
        if self.llm is None:
            diagnosis_state = self._merge_diagnosis_state(state, {"validation_result": False})
            return "方案校验跳过：LLM 未配置。", diagnosis_state
        update = await validate_plan_node(self._diagnosis_state(state), self.llm)
        diagnosis_state = self._merge_diagnosis_state(state, update)
        validation_result = bool(diagnosis_state.get("validation_result"))
        attempts = diagnosis_state.get("validation_attempts", 0)
        return (
            "方案校验完成："
            f"{'通过' if validation_result else '未通过'}，"
            f"校验次数={attempts}。"
        ), diagnosis_state

    async def _summary(self, state: TopicFlowState) -> str:
        if self.llm is not None:
            prompt = _build_summary_prompt(state)
            try:
                logger.info(
                    "Interactive summary LLM start task_id=%s prompt_chars=%s",
                    state.get("task_id"),
                    len(prompt),
                )
                summary = (await self.llm.call(prompt)).strip()
                logger.info(
                    "Interactive summary LLM completed task_id=%s summary_chars=%s",
                    state.get("task_id"),
                    len(summary),
                )
                if summary:
                    return summary
            except Exception:
                logger.exception("Interactive summary LLM failed task_id=%s", state.get("task_id"))
                monitor.record_error("interactive_summary_llm_error")
        await asyncio.sleep(0.1)
        return "总结完成：建议先限流止血，扩容消费者，检查数据库慢查询，并持续观察错误率回落。"

    def _build_final_text(self, state: dict[str, Any]) -> str:
        results = state.get("node_results", [])
        last_result = ""
        for item in results:
            if not isinstance(item, dict):
                continue
            result = str(item.get("result") or "")
            if result:
                last_result = result
            if item.get("node_name") == "summary" and result:
                return result
        if last_result:
            return last_result
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

    def _confirmed_replay_result(self, task: Any, node_name: str) -> str | None:
        if task is None:
            return None
        if task.current_node != node_name:
            return None
        if task.status not in {"confirmed", "retrying"}:
            return None
        if task.pending_node_result is None:
            return None
        return str(task.pending_node_result)

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


def _format_scores(scores: list[float], limit: int = 5) -> str:
    if not scores:
        return "[]"
    return "[" + ", ".join(f"{score:.4f}" for score in scores[:limit]) + (", ..." if len(scores) > limit else "") + "]"


def _format_live_data(live_data: dict[str, Any]) -> str:
    if not live_data:
        return "未获得实时指标、日志或拓扑数据。"
    metrics = live_data.get("metrics") if isinstance(live_data.get("metrics"), dict) else {}
    logs = live_data.get("logs") if isinstance(live_data.get("logs"), dict) else {}
    topology = live_data.get("topology") if isinstance(live_data.get("topology"), dict) else {}
    lines = []
    if metrics:
        lines.append(
            "指标："
            f"p95={metrics.get('latency_p95_ms', '-') }ms，"
            f"错误率={metrics.get('error_rate', '-') }，"
            f"CPU={metrics.get('cpu_usage', '-') }"
        )
    matches = logs.get("matches") if isinstance(logs, dict) else None
    if matches:
        lines.append("日志：" + "；".join(str(item) for item in list(matches)[:2]))
    if topology:
        lines.append(
            "拓扑："
            f"服务={topology.get('service', '-')}，"
            f"依赖={', '.join(str(item) for item in topology.get('dependencies', [])[:5])}"
        )
    return "\n".join(lines) if lines else str(live_data)


def _format_plan_result(plan: object, evidence: list[str]) -> str:
    if not isinstance(plan, dict):
        return f"方案生成完成：{plan}"
    summary = str(plan.get("summary") or plan.get("title") or "已生成诊断方案。")
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    risk_level = str(plan.get("risk_level") or plan.get("risk") or "unknown")
    lines = [f"方案生成完成：{summary}", f"风险级别：{risk_level}"]
    if actions:
        lines.append("建议动作：")
        for index, action in enumerate(actions[:5], start=1):
            lines.append(f"{index}. {action}")
    if evidence:
        lines.append("证据摘要：")
        for item in evidence[-3:]:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _build_summary_prompt(state: TopicFlowState) -> str:
    node_results: list[str] = []
    for item in state.get("node_results", []):
        if not isinstance(item, dict):
            continue
        node_name = str(item.get("node_name") or "unknown")
        result = str(item.get("result") or "")
        node_results.append(f"## {node_name}\n{result[:3000]}")

    context = "\n\n".join(node_results) or "No previous node results."
    return (
        "You are an AIOps incident response assistant for a Feishu interactive alert workflow.\n"
        "请只使用中文 Markdown 输出，内容要适合直接展示在飞书卡片中。\n"
        "不要输出 JSON，不要输出代码块，不要复述原始字典，不要编造不存在的证据。\n"
        "如果证据不足，请在关键证据中明确写“当前证据不足”。\n"
        "处置建议必须可执行，按优先级从高到低排列，并避免危险命令。\n"
        "请严格按以下标题输出：\n"
        "## 故障判断\n"
        "简洁说明最可能的问题。\n\n"
        "## 关键证据\n"
        "- 列出来自缓存、RAG、实时工具调用中的关键证据。\n\n"
        "## 处置建议\n"
        "1. 给出优先级最高的操作。\n"
        "2. 给出后续排查或恢复操作。\n\n"
        "## 风险与观察项\n"
        "- 说明执行动作的风险。\n"
        "- 说明后续需要观察的指标、日志或告警。\n\n"
        f"用户问题:\n{state.get('query', '')}\n\n"
        f"节点结果:\n{context}"
    )
    return (
        "You are an AIOps incident response assistant for a Feishu interactive alert workflow.\n"
        "Respond in concise Chinese Markdown.\n"
        "Use the cache check, RAG retrieval, and live tool results below to produce a practical final diagnosis.\n"
        "Do not invent evidence. If evidence is weak, say so briefly.\n"
        "Output exactly these sections:\n"
        "1. 故障判断\n"
        "2. 关键证据\n"
        "3. 处置建议\n"
        "4. 风险与观察项\n\n"
        f"用户问题:\n{state.get('query', '')}\n\n"
        f"节点结果:\n{context}"
    )
