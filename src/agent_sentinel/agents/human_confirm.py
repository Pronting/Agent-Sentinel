from __future__ import annotations

import logging
import uuid

from agent_sentinel.feishu.card_handler import HumanDecisionStore
from agent_sentinel.feishu.sender import FeishuSender
from agent_sentinel.graph.state import DiagnosisState, append_evidence, append_message

logger = logging.getLogger(__name__)


async def human_confirm_node(
    state: DiagnosisState,
    sender: FeishuSender,
    decision_store: HumanDecisionStore,
    timeout_seconds: int = 300,
    enabled: bool = True,
) -> DiagnosisState:
    logger.info("Node human_confirm started enabled=%s", enabled)
    if not enabled or not state.get("need_human", True) or not state.get("chat_id"):
        logger.info("Node human_confirm auto-approved")
        return {
            "human_decision": "approved",
            "messages": append_message(state, "assistant", "无需人工确认，自动继续。"),
        }

    decision_id = state.get("decision_id") or str(uuid.uuid4())
    await decision_store.register(decision_id)
    await sender.send_card(
        state.get("chat_id"),
        decision_id,
        state.get("recommended_plan", {}),
        state.get("evidence", []),
        thread_root_message_id=state.get("thread_root_message_id"),
    )
    decision = await decision_store.wait_for_decision(decision_id, timeout_seconds)
    logger.info("Node human_confirm completed decision=%s", decision.decision)
    evidence = append_evidence(state, f"Human confirmation decision={decision.decision}.")
    messages = append_message(state, "assistant", f"人工确认结果: {decision.decision}")
    if decision.decision == "rejected" and decision.feedback:
        messages = [*messages, {"role": "user", "content": decision.feedback}]
        evidence = [*evidence, f"Human rejection feedback: {decision.feedback}"]
    return {
        "decision_id": decision_id,
        "human_decision": decision.decision,
        "messages": messages,
        "evidence": evidence,
    }
