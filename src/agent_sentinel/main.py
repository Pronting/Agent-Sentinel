from __future__ import annotations

import argparse
import logging

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query

from agent_sentinel.alerts import FeishuWebhookNotifier, RealtimeAlertService
from agent_sentinel.config import Settings, get_settings
from agent_sentinel.schemas import (
    AlertRecordResponse,
    AlertReportRequest,
    AlertReportResponse,
    ChatRequest,
    ChatResponse,
    HealthResponse,
)
from agent_sentinel.service import SingleTurnChatService


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

    app = FastAPI(title=settings.app_name)

    def get_chat_service() -> SingleTurnChatService:
        nonlocal chat_service
        if chat_service is None:
            chat_service = SingleTurnChatService(settings)
        return chat_service

    def verify_alert_token(provided_token: str | None) -> None:
        expected_token = settings.alert_api_token
        if expected_token and provided_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid alert token.")

    def report_exception(scene: str, exc: Exception) -> None:
        try:
            alert_service.report_exception(scene, exc)
        except Exception as notify_exc:  # pragma: no cover - defensive logging
            logger.exception("Failed to send Feishu alert: %s", notify_exc)

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
