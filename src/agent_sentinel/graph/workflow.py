from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from agent_sentinel.agents.fetch_tools import fetch_live_data_node
from agent_sentinel.agents.final_result import final_result_node
from agent_sentinel.agents.generate_plan import generate_plan_node
from agent_sentinel.agents.human_confirm import human_confirm_node
from agent_sentinel.agents.retrieve_agent import retrieve_node, should_fetch
from agent_sentinel.agents.understand_agent import understand_node
from agent_sentinel.agents.validate_plan import validate_plan_node, validation_result
from agent_sentinel.config import Settings
from agent_sentinel.feishu.card_handler import HumanDecisionStore
from agent_sentinel.feishu.sender import FeishuSender
from agent_sentinel.graph.state import DiagnosisState
from agent_sentinel.llm.executor import LLMExecutor
from agent_sentinel.rag.base import BaseRetriever
from agent_sentinel.rag.factory import build_retriever
from agent_sentinel.utils.config_loader import load_yaml

logger = logging.getLogger(__name__)

NodeFn = Callable[[DiagnosisState], Awaitable[DiagnosisState]]


class DiagnosisWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMExecutor,
        sender: FeishuSender,
        decision_store: HumanDecisionStore,
        retriever: BaseRetriever | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.sender = sender
        self.decision_store = decision_store
        self.retriever = retriever or build_retriever(settings)
        self.checkpointer = checkpointer or InMemorySaver()
        self._compiled: Any | None = None

    def compile(self) -> Any:
        if self._compiled is not None:
            return self._compiled
        workflow_config = load_yaml(self.settings.aiops_workflow_config_path)
        graph = StateGraph(DiagnosisState)
        registry = self._node_registry()

        nodes = workflow_config.get("nodes", [])
        if not nodes:
            raise ValueError("workflow.yaml must define nodes.")
        for item in nodes:
            name = str(item["name"])
            graph.add_node(name, registry[name])

        graph.set_entry_point(str(nodes[0]["name"]))

        for edge in workflow_config.get("edges", []):
            graph.add_edge(str(edge["from"]), str(edge["to"]))

        route_registry = self._route_registry()
        for edge in workflow_config.get("conditional_edges", []):
            source = str(edge["from"])
            condition_name = str(edge["condition"])
            mapping = {
                str(key): END if value == "END" else str(value)
                for key, value in dict(edge["mapping"]).items()
            }
            graph.add_conditional_edges(source, route_registry[condition_name], mapping)

        self._compiled = graph.compile(checkpointer=self.checkpointer)
        logger.info("LangGraph workflow compiled nodes=%s", [item["name"] for item in nodes])
        return self._compiled

    async def run_streaming(self, initial_state: DiagnosisState) -> DiagnosisState:
        app = self.compile()
        workflow_thread_id = initial_state.get("workflow_thread_id", "")
        config = {
            "configurable": {"thread_id": workflow_thread_id},
            "recursion_limit": 20,
        }
        final_state: DiagnosisState = dict(initial_state)
        async for update in app.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            for node_name, node_update in update.items():
                if isinstance(node_update, dict):
                    final_state.update(node_update)
                await self._send_progress(node_name, final_state)
        state_snapshot = await app.aget_state(config)
        final_values = getattr(state_snapshot, "values", None)
        if isinstance(final_values, dict):
            final_state.update(final_values)
        return final_state

    async def resume(self, workflow_thread_id: str, resume_payload: dict[str, Any]) -> DiagnosisState:
        app = self.compile()
        config = {
            "configurable": {"thread_id": workflow_thread_id},
            "recursion_limit": 20,
        }
        state_snapshot = await app.aget_state(config)
        final_state: DiagnosisState = {}
        values = getattr(state_snapshot, "values", None)
        if isinstance(values, dict):
            final_state.update(values)
        async for update in app.astream(
            Command(resume=resume_payload),
            config=config,
            stream_mode="updates",
        ):
            for node_name, node_update in update.items():
                if isinstance(node_update, dict):
                    final_state.update(node_update)
                await self._send_progress(node_name, final_state)
        state_snapshot = await app.aget_state(config)
        final_values = getattr(state_snapshot, "values", None)
        if isinstance(final_values, dict):
            final_state.update(final_values)
        return final_state

    async def update_state(self, workflow_thread_id: str, state_update: dict[str, Any]) -> None:
        app = self.compile()
        await app.aupdate_state(
            {"configurable": {"thread_id": workflow_thread_id}, "recursion_limit": 20},
            state_update,
        )

    def _node_registry(self) -> dict[str, NodeFn]:
        return {
            "understand": partial(understand_node, llm=self.llm),
            "retrieve": partial(retrieve_node, retriever=self.retriever),
            "fetch_live_data": fetch_live_data_node,
            "generate_plan": partial(generate_plan_node, llm=self.llm),
            "validate": partial(validate_plan_node, llm=self.llm),
            "human_confirm": partial(
                human_confirm_node,
                sender=self.sender,
                decision_store=self.decision_store,
                timeout_seconds=self.settings.aiops_human_confirm_timeout_seconds,
                enabled=self.settings.aiops_human_confirm_enabled,
            ),
            "final_result": partial(final_result_node, sender=self.sender),
        }

    def _route_registry(self) -> dict[str, Callable[[DiagnosisState], str]]:
        return {
            "should_fetch": should_fetch,
            "validation_result": validation_result,
            "human_decision": lambda state: state.get("human_decision", "timeout"),
            "always": lambda state: "next",
            "end": lambda state: "end",
        }

    async def _send_progress(self, node_name: str, state: DiagnosisState) -> None:
        status = {
            "understand": "✅ 已完成告警理解，正在检索历史案例...",
            "retrieve": "✅ 历史案例检索完成，正在判断是否需要实时数据...",
            "fetch_live_data": "✅ 实时指标、日志、拓扑已获取，正在生成诊断方案...",
            "generate_plan": "✅ 诊断方案已生成，正在进行安全校验...",
            "validate": "✅ 方案校验完成，等待人工确认...",
            "human_confirm": "✅ 人工确认流程结束，正在发送最终结果...",
            "final_result": "✅ 最终诊断结果已发送。",
        }.get(node_name)
        if not status:
            return
        logger.info(
            "Sending workflow progress node=%s chat_id=%s thread_root_message_id=%s status=%s",
            node_name,
            state.get("chat_id"),
            state.get("thread_root_message_id"),
            status,
        )
        await self.sender.send_message(
            state.get("chat_id"),
            status,
            thread_root_message_id=state.get("thread_root_message_id"),
        )


def build_llm_executor(settings: Settings) -> LLMExecutor:
    return LLMExecutor(
        models=settings.aiops_llm_models,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.openai_temperature,
        trust_env=settings.openai_http_trust_env,
        timeout_seconds=settings.aiops_llm_timeout_seconds,
        max_retries=settings.aiops_llm_max_retries,
        mock_enabled=settings.aiops_mock_llm_enabled,
    )
