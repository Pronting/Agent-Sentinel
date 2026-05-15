from fastapi.testclient import TestClient

from agent_sentinel.config import Settings
from agent_sentinel.main import build_app


def test_health_endpoint() -> None:
    app = build_app(
        Settings(
            app_name="Agent Sentinel",
            app_host="127.0.0.1",
            app_port=8000,
            app_env="test",
            log_level="INFO",
            openai_api_key="test-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_temperature=0.0,
            openai_http_trust_env=False,
            feishu_webhook_url=None,
            feishu_secret=None,
            feishu_alert_enabled=False,
            feishu_alert_title_prefix="Agent Sentinel",
            alert_api_token="test-token",
            alert_dedup_window_seconds=60,
            alert_store_limit=100,
        )
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "Agent Sentinel"}


def test_report_alert_endpoint_records_event() -> None:
    app = build_app(
        Settings(
            app_name="Agent Sentinel",
            app_host="127.0.0.1",
            app_port=8000,
            app_env="test",
            log_level="INFO",
            openai_api_key="test-key",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-5.4",
            openai_temperature=0.0,
            openai_http_trust_env=False,
            feishu_webhook_url=None,
            feishu_secret=None,
            feishu_alert_enabled=False,
            feishu_alert_title_prefix="Agent Sentinel",
            alert_api_token="test-token",
            alert_dedup_window_seconds=60,
            alert_store_limit=100,
        )
    )
    client = TestClient(app)

    response = client.post(
        "/alerts/report",
        headers={"X-Alert-Token": "test-token"},
        json={
            "source": "unit-test",
            "level": "ERROR",
            "summary": "Synthetic alert",
            "details": "Testing realtime alert pipeline.",
            "dedupe_key": "unit-test-error",
            "tags": ["test"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "skipped",
        "dispatched": False,
        "deduplicated": False,
    }

    recent = client.get("/alerts/recent", headers={"X-Alert-Token": "test-token"})
    assert recent.status_code == 200
    assert recent.json()[0]["source"] == "unit-test"
