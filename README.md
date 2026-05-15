# Agent Sentinel

一个基于 LangChain 的最小可运行告警分析服务，支持：

- 单轮对话接口 `POST /chat/once`
- 飞书 webhook 告警推送
- 告警上报接口 `POST /alerts/report`
- 告警分析接口 `POST /alerts/analyze`
- 飞书企业自建应用事件入口 `POST /feishu/events`

## 项目结构

```text
.
├─ .env.example
├─ .gitignore
├─ pyproject.toml
├─ README.md
├─ tests/
└─ src/
   └─ agent_sentinel/
      ├─ alerts.py
      ├─ config.py
      ├─ feishu_app.py
      ├─ llm.py
      ├─ main.py
      ├─ schemas.py
      └─ service.py
```

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

## 环境变量

复制 `.env.example` 为 `.env`，然后补充你自己的密钥。

LLM 相关：

- `OPENAI_API_KEY`
- `CODEX_BASE_URL` 或 `OPENAI_BASE_URL`
- `CODEX_MODEL` 或 `OPENAI_MODEL`
- `OPENAI_HTTP_TRUST_ENV=false`

飞书企业自建应用相关：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_EVENT_VERIFICATION_TOKEN`
- `FEISHU_EVENT_ENCRYPT_KEY`
- `FEISHU_BOT_NAME`
- `FEISHU_ALLOWED_CHAT_IDS`
- `FEISHU_ANALYZE_MENTION_ONLY=true`
- `FEISHU_LONG_CONNECTION_ENABLED=false`
- `FEISHU_MESSAGE_POLLING_ENABLED=true`
- `FEISHU_MESSAGE_POLLING_INTERVAL_SECONDS=5`
- `FEISHU_MESSAGE_POLLING_PAGE_SIZE=20`

飞书 webhook 告警相关：

- `FEISHU_WEBHOOK_URL`
- `FEISHU_SECRET`

分析与告警相关：

- `ALERT_API_TOKEN`
- `ALERT_ANALYSIS_ENABLED=true`
- `ALERT_ANALYSIS_TITLE_PREFIX=Alert Analysis`

说明：

- 当前脚手架默认支持未加密的飞书事件回调。
- 如果你暂时不打算处理事件加密，可先让 `FEISHU_EVENT_ENCRYPT_KEY` 留空，并在飞书后台关闭事件加密。
- 如果你没有公网域名，可以直接启用“定时轮询拉消息”模式，不依赖回调和公网入口。

## 启动

```bash
.\.venv\Scripts\python.exe -m agent_sentinel.main
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

单轮对话：

```bash
curl -X POST http://127.0.0.1:8000/chat/once ^
  -H "Content-Type: application/json" ^
  -d "{\"message\":\"你好，请用一句话介绍你自己\"}"
```

## 飞书后台推荐配置

分析 bot 使用企业自建应用：

1. 开启机器人能力
2. 开启事件订阅
3. 没有公网域名时，推荐开启当前项目的“消息轮询”模式
4. 给应用加读取群消息历史和发送消息相关权限
5. 发布应用版本并把机器人拉进群

## 两条触发链

### 1. 人在群里 @分析bot

轮询模式下，服务启动后会定时调用 `GET /open-apis/im/v1/messages`
按 `FEISHU_ALLOWED_CHAT_IDS` 逐个拉取群历史消息，然后筛出 `@分析bot`
或包含机器人名称的文本消息。

当前代码会：

1. 拉取指定群最新消息
2. 基于 `message_id` 去重
3. 读取群消息文本
4. 调 LLM 做告警分析
5. 再把分析结果发回同一个群

### 2. 告警 bot 直接调用分析接口

请求示例：

```bash
curl -X POST http://127.0.0.1:8000/alerts/analyze ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: your-alert-token" ^
  -d "{\"chat_id\":\"oc_xxx\",\"source\":\"alert-bot\",\"level\":\"ERROR\",\"summary\":\"订单同步失败\",\"details\":\"timeout after 3 retries\",\"raw_text\":\"[ALERT] order sync timeout\",\"trigger_type\":\"bot_alert\",\"tags\":[\"job\",\"prod\"]}"
```

这个接口会：

1. 调 LLM 分析告警
2. 用企业自建应用身份拿 `tenant_access_token`
3. 调飞书发消息 API，把分析结果发回指定群

## 其他接口

测试 webhook 告警：

```bash
curl -X POST http://127.0.0.1:8000/alerts/test
```

上报原始告警：

```bash
curl -X POST http://127.0.0.1:8000/alerts/report ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: your-alert-token" ^
  -d "{\"source\":\"nightly-job\",\"level\":\"ERROR\",\"summary\":\"Nightly sync failed\",\"details\":\"timeout after retries\"}"
```

查看最近告警：

```bash
curl http://127.0.0.1:8000/alerts/recent ^
  -H "X-Alert-Token: your-alert-token"
```
