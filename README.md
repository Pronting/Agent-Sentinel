# Agent Sentinel

一个最小可运行的 LangChain 项目骨架，包含：

- 单轮对话接口 `POST /chat/once`
- 命令行单次提问模式
- 飞书机器人实时告警转发
- 统一告警上报接口 `POST /alerts/report`
- `.env.example` 与基础工程配置

## 目录结构

```text
.
├─ .env.example
├─ .gitignore
├─ pyproject.toml
├─ README.md
└─ src/
   └─ agent_sentinel/
      ├─ __init__.py
      ├─ alerts.py
      ├─ config.py
      ├─ llm.py
      ├─ main.py
      ├─ schemas.py
      └─ service.py
```

## 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

如果你还要开发测试：

```bash
pip install -e .[dev]
```

## 2. 配置环境变量

复制 `.env.example` 为 `.env`，然后填入你自己的密钥和飞书机器人配置。

关键变量：

- `OPENAI_API_KEY`: 大模型 API Key
- `CODEX_BASE_URL`: 兼容 OpenAI 协议的自定义模型服务地址，已支持作为 `OPENAI_BASE_URL` 的别名
- `OPENAI_BASE_URL`: OpenAI 或兼容服务地址
- `OPENAI_MODEL`: 模型名，例如 `gpt-4o-mini`
- `FEISHU_WEBHOOK_URL`: 飞书群机器人 webhook
- `FEISHU_SECRET`: 如果机器人启用了签名校验则填写
- `ALERT_API_TOKEN`: 告警上报接口鉴权 token
- `ALERT_DEDUP_WINDOW_SECONDS`: 相同 `dedupe_key` 的短时间去重窗口

## 3. 启动服务

```bash
agent-sentinel
```

默认会启动在 `http://0.0.0.0:8000`。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

单轮对话：

```bash
curl -X POST http://127.0.0.1:8000/chat/once \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"你好，请用一句话介绍你自己\"}"
```

## 4. 命令行单次提问

```bash
python -m agent_sentinel.main --message "你好，请做一个一句话自我介绍"
```

## 5. 测试飞书告警

```bash
curl -X POST http://127.0.0.1:8000/alerts/test
```

如果服务处理异常，会自动尝试向飞书推送错误告警。

## 6. 实时告警上报到飞书群

这个服务现在支持作为“统一告警入口”使用。任何外部任务、脚本、服务，只要发现告警，就可以立刻调用：

```bash
curl -X POST http://127.0.0.1:8000/alerts/report ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: your-alert-token" ^
  -d "{\"source\":\"nightly-job\",\"level\":\"ERROR\",\"summary\":\"Nightly sync failed\",\"details\":\"order sync timeout after 3 retries\",\"dedupe_key\":\"nightly-job-sync-failed\",\"tags\":[\"job\",\"prod\"]}"
```

字段说明：

- `source`: 告警来源，例如服务名、任务名
- `level`: `INFO`、`WARNING`、`ERROR`、`CRITICAL`
- `summary`: 告警摘要
- `details`: 详细信息，可选
- `dedupe_key`: 去重键，可选；在去重窗口内重复上报会被压制
- `tags`: 标签列表，可选

查看最近收到的告警：

```bash
curl http://127.0.0.1:8000/alerts/recent ^
  -H "X-Alert-Token: your-alert-token"
```

这意味着“实时监听”通常由你的业务系统或定时任务负责发现异常，而 `Agent Sentinel` 负责接收这些告警并立即转发到飞书群。
