from __future__ import annotations

import argparse
import logging
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request

from agent_sentinel.alerts import FeishuWebhookNotifier, RealtimeAlertService
from agent_sentinel.config import Settings, get_settings
from agent_sentinel.feishu_app import FeishuBotClient, extract_text_from_message_content
from agent_sentinel.feishu.card_handler import FeishuCardHandler, HumanDecisionStore
from agent_sentinel.feishu.sender import FeishuSender
from agent_sentinel.feishu_longconn import FeishuLongConnectionBot
from agent_sentinel.feishu_poller import FeishuMessagePoller
from agent_sentinel.graph.state import DiagnosisState
from agent_sentinel.graph.workflow import DiagnosisWorkflow, build_llm_executor
from agent_sentinel.schemas import (
    AlertAnalyzeRequest,
    AlertAnalyzeResponse,
    AlertRecordResponse,
    AlertReportRequest,
    AlertReportResponse,
    ChatRequest,
    ChatResponse,
    FeishuEventEnvelope,
    HealthResponse,
)
from agent_sentinel.service import AlertAnalysisService, SingleTurnChatService


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    notifier = FeishuWebhookNotifier(
        webhook_url=settings.feishu_webhook_url,
        secret=settings.feishu_secret,
        enabled=settings.feishu_alert_enabled,
    )
    alert_service = RealtimeAlertService(
        notifier=notifier,
        app_env=settings.app_env,
        title_prefix=settings.feishu_alert_title_prefix,
        dedup_window_seconds=settings.alert_dedup_window_seconds,
        store_limit=settings.alert_store_limit,
    )

    chat_service: SingleTurnChatService | None = None
    analysis_service: AlertAnalysisService | None = None
    feishu_bot_client = FeishuBotClient(settings)
    feishu_sender = FeishuSender(feishu_bot_client)
    decision_store = HumanDecisionStore(settings.redis_url)
    card_handler = FeishuCardHandler(decision_store)
    aiops_workflow: DiagnosisWorkflow | None = None
    longconn_bot: FeishuLongConnectionBot | None = None
    poller_bot: FeishuMessagePoller | None = None

    app = FastAPI(title=settings.app_name)

    def get_chat_service() -> SingleTurnChatService:
        nonlocal chat_service
        if chat_service is None:
            chat_service = SingleTurnChatService(settings)
        return chat_service

    def get_analysis_service() -> AlertAnalysisService:
        nonlocal analysis_service
        if analysis_service is None:
            analysis_service = AlertAnalysisService(settings)
        return analysis_service

    def get_aiops_workflow() -> DiagnosisWorkflow:
        nonlocal aiops_workflow
        if aiops_workflow is None:
            aiops_workflow = DiagnosisWorkflow(
                settings=settings,
                llm=build_llm_executor(settings),
                sender=feishu_sender,
                decision_store=decision_store,
            )
        return aiops_workflow

    def verify_alert_token(provided_token: str | None) -> None:
        expected_token = settings.alert_api_token
        if expected_token and provided_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid alert token.")

    def verify_feishu_event_token(payload: FeishuEventEnvelope) -> None:
        expected_token = settings.feishu_event_verification_token
        if not expected_token:
            return

        provided_token = None
        if payload.header and payload.header.token:
            provided_token = payload.header.token
        elif payload.token:
            provided_token = payload.token

        if provided_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid Feishu event token.")

    def report_exception(scene: str, exc: Exception) -> None:
        try:
            alert_service.report_exception(scene, exc)
        except Exception as notify_exc:  # pragma: no cover
            logger.exception("Failed to send Feishu alert for %s: %s", scene, notify_exc)

    def build_analysis_message(source: str, level: str, summary: str, analysis: str) -> str:
        return (
            f"[{settings.alert_analysis_title_prefix}]\n"
            f"Source: {source}\n"
            f"Level: {level}\n"
            f"Summary: {summary}\n\n"
            f"{analysis}"
        )

    def analyze_and_send_to_chat(
        chat_id: str,
        source: str,
        level: str,
        summary: str,
        details: str | None = None,
        raw_text: str | None = None,
        trigger_type: str = "unknown",
        tags: list[str] | None = None,
        thread_root_message_id: str | None = None,
        mention_open_id: str | None = None,
        mention_name: str | None = None,
    ) -> tuple[str, bool]:
        if not settings.alert_analysis_enabled:
            return "Alert analysis is disabled.", False

        analysis = get_analysis_service().analyze_alert(
            source=source,
            level=level,
            summary=summary,
            details=details,
            raw_text=raw_text,
            trigger_type=trigger_type,
            tags=tags,
        )
        sent = feishu_bot_client.send_text_to_chat(
            chat_id,
            build_analysis_message(source, level, summary, analysis),
            thread_root_message_id=thread_root_message_id,
            mention_open_id=mention_open_id,
            mention_name=mention_name,
        )
        return analysis, sent

    @app.on_event("startup")
    def startup_event() -> None:
        nonlocal longconn_bot, poller_bot
        if longconn_bot is None:
            longconn_bot = FeishuLongConnectionBot(
                settings=settings,
                analyze_callback=analyze_and_send_to_chat,
            )
        longconn_bot.start()

        if poller_bot is None:
            poller_bot = FeishuMessagePoller(
                settings=settings,
                feishu_bot_client=feishu_bot_client,
                analyze_callback=analyze_and_send_to_chat,
            )
        poller_bot.start()

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", app=settings.app_name)

    @app.post("/chat/once", response_model=ChatResponse)
    def chat_once(payload: ChatRequest) -> ChatResponse:
        try:
            answer = get_chat_service().reply_once(payload.message)
            return ChatResponse(answer=answer, model=settings.openai_model)
        except Exception as exc:
            logger.exception("Single-turn chat failed")
            report_exception("/chat/once", exc)
            raise HTTPException(status_code=500, detail="Chat request failed.") from exc

    @app.post("/alerts/test")
    def test_alert() -> dict[str, str]:
        try:
            dispatched, deduplicated = alert_service.report(
                source="agent-sentinel",
                level="INFO",
                summary="Manual alert test",
                details=f"Application {settings.app_name} triggered a manual alert test.",
                tags=["manual-test"],
            )
            if deduplicated:
                return {"status": "deduplicated"}
            return {"status": "sent" if dispatched else "skipped"}
        except Exception as exc:
            logger.exception("Failed to send test alert")
            raise HTTPException(status_code=500, detail="Failed to send alert.") from exc

    @app.post("/alerts/report", response_model=AlertReportResponse)
    def report_alert(
        payload: AlertReportRequest,
        x_alert_token: str | None = Header(default=None),
    ) -> AlertReportResponse:
        try:
            verify_alert_token(x_alert_token)
            dispatched, deduplicated = alert_service.report(
                source=payload.source,
                level=payload.level,
                summary=payload.summary,
                details=payload.details,
                dedupe_key=payload.dedupe_key,
                tags=payload.tags,
            )
            status = "deduplicated" if deduplicated else "sent" if dispatched else "skipped"
            return AlertReportResponse(
                status=status,
                dispatched=dispatched,
                deduplicated=deduplicated,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to report alert")
            raise HTTPException(status_code=500, detail="Failed to report alert.") from exc

    @app.get("/alerts/recent", response_model=list[AlertRecordResponse])
    def recent_alerts(
        limit: int = Query(default=20, ge=1, le=100),
        x_alert_token: str | None = Header(default=None),
    ) -> list[AlertRecordResponse]:
        verify_alert_token(x_alert_token)
        return [AlertRecordResponse(**item) for item in alert_service.recent_alerts(limit=limit)]

    @app.post("/alerts/analyze", response_model=AlertAnalyzeResponse)
    def analyze_alert(
        payload: AlertAnalyzeRequest,
        x_alert_token: str | None = Header(default=None),
    ) -> AlertAnalyzeResponse:
        try:
            verify_alert_token(x_alert_token)
            thread_root_message_id = payload.thread_root_message_id or payload.message_id
            analysis, sent = analyze_and_send_to_chat(
                chat_id=payload.chat_id,
                source=payload.source,
                level=payload.level,
                summary=payload.summary,
                details=payload.details,
                raw_text=payload.raw_text,
                trigger_type=payload.trigger_type,
                tags=payload.tags,
                thread_root_message_id=thread_root_message_id,
                mention_open_id=payload.mention_open_id,
                mention_name=payload.mention_name,
            )
            return AlertAnalyzeResponse(
                status="sent" if sent else "generated",
                analysis=analysis,
                sent_to_feishu=sent,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to analyze alert")
            report_exception("/alerts/analyze", exc)
            raise HTTPException(status_code=500, detail="Failed to analyze alert.") from exc

    @app.post("/aiops/diagnose")
    async def aiops_diagnose(
        payload: AlertAnalyzeRequest,
        x_alert_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            verify_alert_token(x_alert_token)
            initial_state: DiagnosisState = {
                "raw_alert": {
                    "source": payload.source,
                    "level": payload.level,
                    "summary": payload.summary,
                    "details": payload.details,
                    "raw_text": payload.raw_text,
                    "trigger_type": payload.trigger_type,
                    "tags": payload.tags,
                },
                "chat_id": payload.chat_id,
                "thread_root_message_id": payload.thread_root_message_id or payload.message_id,
                "mention_open_id": payload.mention_open_id,
                "mention_name": payload.mention_name,
                "messages": [],
                "evidence": [],
                "retrieved_docs": [],
                "live_data": {},
                "recommended_plan": {},
                "validation_result": False,
                "need_human": True,
            }
            final_state = await get_aiops_workflow().run_streaming(initial_state)
            return {
                "status": "ok",
                "summary": final_state.get("alert_summary", ""),
                "recommended_plan": final_state.get("recommended_plan", {}),
                "evidence": final_state.get("evidence", []),
                "validation_result": final_state.get("validation_result", False),
                "human_decision": final_state.get("human_decision", "unknown"),
                "final_text": final_state.get("final_text", ""),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("AIOps diagnosis failed")
            report_exception("/aiops/diagnose", exc)
            raise HTTPException(status_code=500, detail="AIOps diagnosis failed.") from exc

    @app.post("/feishu/card/callback")
    async def feishu_card_callback(request: Request) -> dict[str, str]:
        try:
            payload = await request.json()
            return await card_handler.handle(payload)
        except Exception as exc:
            logger.exception("Failed to process Feishu card callback")
            raise HTTPException(status_code=500, detail="Failed to process card callback.") from exc

    @app.post("/feishu/events")
    def feishu_events(payload: FeishuEventEnvelope) -> dict[str, object]:
        try:
            if payload.challenge:
                return {"challenge": payload.challenge}
            if payload.type == "url_verification":
                return {"challenge": payload.challenge or ""}

            verify_feishu_event_token(payload)

            if settings.feishu_event_encrypt_key and payload.event is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Encrypted Feishu events are not supported in this scaffold yet. "
                        "Disable event encryption or add decryption support."
                    ),
                )

            header = payload.header
            if header is None or header.event_type != "im.message.receive_v1":
                return {"status": "ignored"}

            event = payload.event or {}
            sender = event.get("sender") or {}
            sender_type = sender.get("sender_type")
            if sender_type and sender_type != "user":
                return {"status": "ignored"}

            message = event.get("message") or {}
            chat_id = str(message.get("chat_id") or "")
            if not chat_id:
                return {"status": "ignored"}

            if settings.feishu_allowed_chat_ids and chat_id not in settings.feishu_allowed_chat_ids:
                return {"status": "ignored"}

            mentions = message.get("mentions") or []
            if settings.feishu_analyze_mention_only and not mentions:
                return {"status": "ignored"}

            content_text = extract_text_from_message_content(message.get("content"))
            if not content_text.strip():
                return {"status": "ignored"}

            sender_open_id = None
            sender_name = None
            if isinstance(sender, dict):
                sender_open_id = str(sender.get("sender_id") or sender.get("id") or "").strip() or None
                sender_name = str(sender.get("name") or "").strip() or None

            thread_root_message_id = (
                str(message.get("root_id") or "").strip()
                or str(message.get("message_id") or "").strip()
                or None
            )
            analysis, sent = analyze_and_send_to_chat(
                chat_id=chat_id,
                source="feishu-user",
                level="INFO",
                summary=f"{settings.feishu_bot_name} received a mention request",
                details=content_text,
                raw_text=content_text,
                trigger_type="user_message",
                tags=["feishu", "mention"],
                thread_root_message_id=thread_root_message_id,
                mention_open_id=sender_open_id,
                mention_name=sender_name,
            )
            return {"status": "ok", "sent_to_feishu": sent, "analysis_preview": analysis[:120]}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to process Feishu event")
            report_exception("/feishu/events", exc)
            raise HTTPException(status_code=500, detail="Failed to process Feishu event.") from exc

    return app


def run_cli_once(settings: Settings, message: str) -> int:
    chat_service = SingleTurnChatService(settings)
    answer = chat_service.reply_once(message)
    print(answer)
    return 0


def run() -> int:
    parser = argparse.ArgumentParser(description="Agent Sentinel runner")
    parser.add_argument("--message", help="Run a single-turn chat in CLI mode.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    if args.message:
        return run_cli_once(settings, args.message)

    app = build_app(settings)
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
