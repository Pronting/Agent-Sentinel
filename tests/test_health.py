from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from agent_sentinel.config import Settings
from agent_sentinel.feishu_app import FeishuBotClient
from agent_sentinel.main import build_app


def make_settings() -> Settings:
    return Settings(
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
        feishu_api_base_url="https://open.feishu.cn",
        feishu_app_id="app-id",
        feishu_app_secret="app-secret",
        feishu_event_verification_token="verify-token",
        feishu_event_encrypt_key=None,
        feishu_bot_name="Analysis Bot",
        feishu_allowed_chat_ids=[],
        feishu_analyze_mention_only=True,
        feishu_long_connection_enabled=False,
        feishu_message_polling_enabled=False,
        feishu_message_polling_interval_seconds=5,
        feishu_message_polling_page_size=20,
        feishu_webhook_url=None,
        feishu_secret=None,
        feishu_alert_enabled=False,
        feishu_alert_title_prefix="Agent Sentinel",
        alert_analysis_enabled=True,
        alert_analysis_title_prefix="Alert Analysis",
        alert_api_token="test-token",
        alert_dedup_window_seconds=60,
        alert_store_limit=100,
    )


def test_health_endpoint() -> None:
    app = build_app(make_settings())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "Agent Sentinel"}


def test_report_alert_endpoint_records_event() -> None:
    app = build_app(make_settings())
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


def test_feishu_event_challenge() -> None:
    app = build_app(make_settings())
    client = TestClient(app)

    response = client.post("/feishu/events", json={"challenge": "hello-feishu"})

    assert response.status_code == 200
    assert response.json() == {"challenge": "hello-feishu"}


def test_alert_analyze_endpoint_prefers_thread_root_message_id() -> None:
    settings = make_settings()
    with (
        patch("agent_sentinel.main.AlertAnalysisService") as analysis_cls,
        patch("agent_sentinel.main.FeishuBotClient") as bot_cls,
    ):
        analysis_cls.return_value.analyze_alert.return_value = "[Alert Analysis]\nSeverity: High"
        bot_cls.return_value.send_text_to_chat.return_value = True

        app = build_app(settings)
        client = TestClient(app)

        response = client.post(
            "/alerts/analyze",
            headers={"X-Alert-Token": "test-token"},
            json={
                "chat_id": "oc_test_chat",
                "message_id": "om_original_message",
                "thread_root_message_id": "om_thread_root",
                "source": "alert-bot",
                "level": "ERROR",
                "summary": "Synthetic alert",
                "details": "timeout after retries",
                "raw_text": "[ALERT] timeout",
                "trigger_type": "bot_alert",
                "tags": ["bot"],
                "mention_open_id": "ou_user",
                "mention_name": "Fe",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "sent"
        assert response.json()["sent_to_feishu"] is True
        bot_cls.return_value.send_text_to_chat.assert_called_once()
        _, kwargs = bot_cls.return_value.send_text_to_chat.call_args
        assert kwargs["thread_root_message_id"] == "om_thread_root"
        assert kwargs["mention_open_id"] == "ou_user"
        assert kwargs["mention_name"] == "Fe"


def test_feishu_bot_client_uses_reply_endpoint_for_thread_reply() -> None:
    settings = make_settings()
    client = FeishuBotClient(settings)

    token_response = Mock()
    token_response.raise_for_status.return_value = None
    token_response.json.return_value = {
        "code": 0,
        "tenant_access_token": "tenant-token",
        "expire": 7200,
    }

    send_response = Mock()
    send_response.raise_for_status.return_value = None
    send_response.json.return_value = {"code": 0}

    with patch("agent_sentinel.feishu_app.requests.post", side_effect=[token_response, send_response]) as post_mock:
        result = client.send_text_to_chat(
            "oc_test_chat",
            "analysis body",
            thread_root_message_id="om_thread_root",
            mention_open_id="ou_user",
            mention_name="Fe",
        )

    assert result is True
    assert post_mock.call_count == 2
    send_args, send_kwargs = post_mock.call_args_list[1]
    assert send_args[0].endswith("/open-apis/im/v1/messages/om_thread_root/reply")
    assert send_kwargs["json"]["receive_id"] == "oc_test_chat"
