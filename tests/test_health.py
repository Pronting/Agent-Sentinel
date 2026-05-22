from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from agent_sentinel.config import Settings
from agent_sentinel.feishu_app import FeishuBotClient
from agent_sentinel.interactive_topic.topic_sender import build_workflow_card
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
        interactive_topic_enabled=False,
        interactive_topic_wait_seconds=5,
    )


def test_health_endpoint() -> None:
    app = build_app(make_settings())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "Agent Sentinel"}


def test_metrics_endpoint_exposes_prometheus_text() -> None:
    app = build_app(make_settings())
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "workflow_node_duration_seconds" in response.text or "prometheus_client is not installed" in response.text


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
    with patch("agent_sentinel.main.DiagnosisWorkflow") as workflow_cls:
        workflow_cls.return_value.run_streaming.return_value = {
            "alert_summary": "Synthetic alert summary",
            "recommended_plan": {"summary": "Use workflow path"},
            "evidence": ["workflow evidence"],
            "validation_result": True,
            "human_decision": "approved",
            "final_text": "[AIOps Diagnosis] final",
        }
        workflow_cls.return_value.run_streaming = AsyncMock(
            return_value=workflow_cls.return_value.run_streaming.return_value
        )

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
        assert response.json()["analysis"] == "[AIOps Diagnosis] final"
        workflow_cls.return_value.run_streaming.assert_called_once()
        state = workflow_cls.return_value.run_streaming.call_args.args[0]
        assert state["chat_id"] == "oc_test_chat"
        assert state["thread_root_message_id"] == "om_thread_root"
        assert state["mention_open_id"] == "ou_user"
        assert state["mention_name"] == "Fe"
        assert state["raw_alert"]["trigger_type"] == "bot_alert"


def test_feishu_event_routes_into_langgraph_workflow() -> None:
    settings = make_settings()
    settings.feishu_allowed_chat_ids = ["oc_test_chat"]
    with patch("agent_sentinel.main.DiagnosisWorkflow") as workflow_cls:
        workflow_cls.return_value.run_streaming.return_value = {
            "alert_summary": "Feishu alert summary",
            "recommended_plan": {"summary": "Use workflow path"},
            "evidence": ["workflow evidence"],
            "validation_result": True,
            "human_decision": "approved",
            "final_text": "[AIOps Diagnosis] final",
        }
        workflow_cls.return_value.run_streaming = AsyncMock(
            return_value=workflow_cls.return_value.run_streaming.return_value
        )

        app = build_app(settings)
        client = TestClient(app)

        response = client.post(
            "/feishu/events",
            json={
                "schema": "2.0",
                "header": {
                    "event_type": "im.message.receive_v1",
                    "token": "verify-token",
                },
                "event": {
                    "sender": {
                        "sender_type": "user",
                        "sender_id": "ou_user",
                        "name": "Fe",
                    },
                    "message": {
                        "chat_id": "oc_test_chat",
                        "message_id": "om_original_message",
                        "root_id": "om_thread_root",
                        "mentions": [{"name": "Analysis Bot"}],
                        "content": '{"text":"@bot help"}',
                    },
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["sent_to_feishu"] is True
        workflow_cls.return_value.run_streaming.assert_called_once()
        state = workflow_cls.return_value.run_streaming.call_args.args[0]
        assert state["chat_id"] == "oc_test_chat"
        assert state["thread_root_message_id"] == "om_thread_root"
        assert state["mention_open_id"] == "ou_user"
        assert state["mention_name"] == "Fe"
        assert state["workflow_thread_id"]
        assert state["workflow_run_id"]
        assert state["raw_alert"]["source"] == "feishu-user"
        assert state["raw_alert"]["details"] == "@bot help"
        assert state["raw_alert"]["trigger_type"] == "user_message"


def test_feishu_event_starts_interactive_topic_workflow() -> None:
    settings = make_settings()
    settings.interactive_topic_enabled = True
    settings.feishu_allowed_chat_ids = ["oc_test_chat"]
    with (
        patch("agent_sentinel.main.InteractiveTopicWorkflow") as workflow_cls,
        patch("agent_sentinel.main.build_retriever") as build_retriever,
    ):
        retriever = Mock()
        build_retriever.return_value = retriever
        workflow_cls.return_value.start = AsyncMock(return_value="topic-task-1")

        app = build_app(settings)
        client = TestClient(app)

        response = client.post(
            "/feishu/events",
            json={
                "schema": "2.0",
                "header": {
                    "event_type": "im.message.receive_v1",
                    "token": "verify-token",
                },
                "event": {
                    "sender": {
                        "sender_type": "user",
                        "sender_id": "ou_user",
                        "name": "Fe",
                    },
                    "message": {
                        "chat_id": "oc_test_chat",
                        "message_id": "om_original_message",
                        "root_id": "om_thread_root",
                        "mentions": [{"name": "Analysis Bot"}],
                        "content": '{"text":"@bot run interactive flow"}',
                    },
                },
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "sent_to_feishu": True, "task_id": "topic-task-1"}
        workflow_cls.return_value.start.assert_awaited_once_with(
            "oc_test_chat",
            "om_thread_root",
            "@bot run interactive flow",
        )
        build_retriever.assert_called_once_with(settings)
        assert workflow_cls.call_args.kwargs["retriever"] is retriever


def test_webhook_card_routes_interactive_topic_callback() -> None:
    settings = make_settings()
    settings.interactive_topic_enabled = True
    with patch("agent_sentinel.main.InteractiveTopicWorkflow") as workflow_cls:
        workflow_cls.return_value.handle_card_callback = AsyncMock(return_value={"status": "ok"})

        app = build_app(settings)
        client = TestClient(app)

        payload = {"action": {"value": {"task_id": "topic-1", "node_name": "cache_check", "action": "next"}}}
        response = client.post("/webhook/card", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        workflow_cls.return_value.handle_card_callback.assert_awaited_once_with(payload, source="http")


def test_interactive_workflow_card_renders_statuses_and_buttons() -> None:
    card = build_workflow_card(
        task_id="topic-1",
        query="CPU 飙升",
        node_statuses={"cache_check": "done", "rag_retrieve": "running"},
        current_node="rag_retrieve",
        current_result="RAG检索正在执行",
        wait_seconds=5,
        buttons_node="rag_retrieve",
    )

    first_markdown = card["elements"][0]["content"]
    assert "✅ 已完成 告警理解" in first_markdown
    assert "🔄 正在执行 RAG检索" in first_markdown
    assert "⏸ 等待中 实时数据" in first_markdown
    action = card["elements"][-1]
    button_texts = [item["text"]["content"] for item in action["actions"]]
    assert button_texts == ["同意", "拒绝"]


def test_feishu_card_callback_resumes_workflow() -> None:
    settings = make_settings()
    with (
        patch("agent_sentinel.main.DiagnosisWorkflow") as workflow_cls,
        patch("agent_sentinel.main.FeishuCardHandler.parse_callback") as parse_callback,
    ):
        parse_callback.return_value = Mock(
            decision_id="decision-1",
            workflow_thread_id="wf-1",
            workflow_run_id="run-1",
            status="approved",
            feedback="",
        )
        parse_callback.side_effect = AsyncMock(return_value=parse_callback.return_value)
        workflow_cls.return_value.resume = AsyncMock(return_value={"human_decision": "approved"})
        workflow_cls.return_value.update_state = AsyncMock(return_value=None)

        app = build_app(settings)
        client = TestClient(app)

        response = client.post(
            "/feishu/card/callback",
            json={
                "action": {
                    "value": {
                        "action": "diagnosis_confirm",
                        "decision": "approved",
                        "decision_id": "decision-1",
                    }
                }
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        workflow_cls.return_value.update_state.assert_not_awaited()
        workflow_cls.return_value.resume.assert_awaited_once_with(
            "wf-1",
            {"decision": "approved", "feedback": ""},
        )


def test_feishu_card_callback_updates_feedback_before_resume() -> None:
    settings = make_settings()
    with (
        patch("agent_sentinel.main.DiagnosisWorkflow") as workflow_cls,
        patch("agent_sentinel.main.FeishuCardHandler.parse_callback") as parse_callback,
    ):
        parse_callback.return_value = Mock(
            decision_id="decision-2",
            workflow_thread_id="wf-2",
            workflow_run_id="run-2",
            status="rejected",
            feedback="请补充风险说明",
        )
        parse_callback.side_effect = AsyncMock(return_value=parse_callback.return_value)
        workflow_cls.return_value.resume = AsyncMock(return_value={"human_decision": "rejected"})
        workflow_cls.return_value.update_state = AsyncMock(return_value=None)

        app = build_app(settings)
        client = TestClient(app)

        response = client.post(
            "/feishu/card/callback",
            json={
                "action": {
                    "value": {
                        "action": "diagnosis_confirm",
                        "decision": "rejected",
                        "decision_id": "decision-2",
                    }
                }
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        workflow_cls.return_value.update_state.assert_awaited_once_with(
            "wf-2",
            {"human_feedback": "请补充风险说明"},
        )
        workflow_cls.return_value.resume.assert_awaited_once_with(
            "wf-2",
            {"decision": "rejected", "feedback": "请补充风险说明"},
        )


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
    assert "receive_id" not in send_kwargs["json"]
    assert send_kwargs["json"]["reply_in_thread"] is True


def test_feishu_bot_client_uses_thread_reply_for_interactive_cards() -> None:
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
        result = client.send_interactive_card_to_chat(
            "oc_test_chat",
            {"config": {"wide_screen_mode": True}},
            thread_root_message_id="om_thread_root",
        )

    assert result is True
    assert post_mock.call_count == 2
    send_args, send_kwargs = post_mock.call_args_list[1]
    assert send_args[0].endswith("/open-apis/im/v1/messages/om_thread_root/reply")
    assert send_kwargs["json"]["msg_type"] == "interactive"
    assert "receive_id" not in send_kwargs["json"]
    assert send_kwargs["json"]["reply_in_thread"] is True


def test_feishu_bot_client_keeps_top_level_send_without_thread_flag() -> None:
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
        result = client.send_text_to_chat("oc_test_chat", "analysis body")

    assert result is True
    assert post_mock.call_count == 2
    send_args, send_kwargs = post_mock.call_args_list[1]
    assert send_args[0].endswith("/open-apis/im/v1/messages?receive_id_type=chat_id")
    assert "reply_in_thread" not in send_kwargs["json"]
