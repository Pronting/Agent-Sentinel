# Agent Sentinel

Agent Sentinel 是一个基于 FastAPI + LangChain + LangGraph 的多 Agent 智能告警诊断服务。它保留原有飞书消息接入、告警上报、简单对话接口，同时新增一条 AIOps 诊断工作流：

1. 飞书或 HTTP 告警接入
2. 告警理解与摘要
3. Mock RAG 历史案例检索
4. 条件路由判断是否需要实时数据
5. 并发调用 Mock 指标、日志、拓扑工具
6. 生成带证据链的诊断方案
7. 规则 + 轻量 LLM 校验
8. 飞书交互卡片人工确认
9. 流式推送中间状态并发送最终结果

## 项目结构

```text
.
├─ config/
│  ├─ settings.yaml
│  ├─ workflow.yaml
│  ├─ .env.example
│  └─ prompts/
│     ├─ understand.yaml
│     ├─ generate_plan.yaml
│     └─ validate.yaml
├─ src/
│  ├─ main.py
│  └─ agent_sentinel/
│     ├─ agents/
│     ├─ feishu/
│     ├─ graph/
│     ├─ llm/
│     ├─ rag/
│     ├─ tools/
│     ├─ utils/
│     └─ main.py
├─ tests/
│  ├─ test_health.py
│  └─ test_workflow.py
├─ Dockerfile
├─ docker-compose.yml
├─ pyproject.toml
└─ requirements.txt
```

## 本地运行

```powershell
cd E:\pythonStudy\code\Agent-Sentinel
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
copy config\.env.example .env
```

先用 Mock LLM 跑通流程时，保持：

```env
AIOPS_MOCK_LLM_ENABLED=true
AIOPS_HUMAN_CONFIRM_ENABLED=false
```

启动服务：

```powershell
.\.venv\Scripts\python.exe -m agent_sentinel.main
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

## AIOps 诊断演示

```powershell
curl -X POST http://127.0.0.1:8000/aiops/diagnose ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: replace-with-a-strong-token" ^
  -d "{\"chat_id\":\"\",\"source\":\"alert-bot\",\"level\":\"ERROR\",\"summary\":\"订单同步超时\",\"details\":\"timeout after 3 retries\",\"raw_text\":\"[ALERT] order sync timeout\",\"trigger_type\":\"bot_alert\",\"tags\":[\"prod\",\"order\"]}"
```

`chat_id` 为空时不会真实发送飞书消息，但会完整执行 LangGraph 流程。接入真实飞书群时填入 `chat_id`，并配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。

## Docker 运行

```powershell
copy config\.env.example .env
docker compose up --build
```

Compose 会启动 Redis 和 app。Redis 当前用于保存人工确认决策，后续可以替换为 LangGraph checkpoint 持久化。

## 关键配置

`config/settings.yaml` 提供默认配置，`.env` 会覆盖敏感项和部署差异：

```env
OPENAI_API_KEY=your-openai-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
AIOPS_LLM_MODELS=gpt-4o-mini,gpt-3.5-turbo
AIOPS_MOCK_LLM_ENABLED=false
AIOPS_HUMAN_CONFIRM_ENABLED=true
AIOPS_HUMAN_CONFIRM_TIMEOUT_SECONDS=300
REDIS_URL=redis://localhost:6379/0
```

多模型降级由 `AIOPS_LLM_MODELS` 控制，按顺序尝试。`LLMExecutor` 会记录模型、耗时、prompt/response 字符数，并在主模型失败后切到备用模型。

## Workflow YAML

`config/workflow.yaml` 描述 LangGraph 拓扑：

```yaml
nodes:
  - name: understand
  - name: retrieve
  - name: fetch_live_data
  - name: generate_plan
  - name: validate
  - name: human_confirm
  - name: final_result
edges:
  - from: understand
    to: retrieve
conditional_edges:
  - from: retrieve
    condition: should_fetch
    mapping:
      fetch: fetch_live_data
      skip: generate_plan
```

代码在 `agent_sentinel.graph.workflow.DiagnosisWorkflow` 中读取该文件，动态注册节点和条件路由。新增节点时，需要在 YAML 中声明，并在 `_node_registry()` 中注册函数。

## Prompt 外部化

节点 Prompt 存放在 `config/prompts/`：

- `understand.yaml`: 告警理解
- `generate_plan.yaml`: 诊断方案与证据链
- `validate.yaml`: 方案校验

Prompt 使用 `{raw_alert}`、`{alert_summary}`、`{live_data}` 等变量渲染，后续可直接替换模板而无需改节点代码。

## 飞书交互卡片

启用人工确认：

```env
AIOPS_HUMAN_CONFIRM_ENABLED=true
AIOPS_HUMAN_CONFIRM_TIMEOUT_SECONDS=300
```

服务会通过 `FeishuSender.send_card()` 发送交互卡片，包含“同意”和“拒绝”按钮。飞书后台卡片回调地址配置为：

```text
POST http(s)://你的域名/feishu/card/callback
```

回调示例：

```json
{
  "action": {
    "value": {
      "action": "diagnosis_confirm",
      "decision_id": "uuid",
      "decision": "approved"
    }
  }
}
```

拒绝时可带 `feedback` 字段，工作流会把反馈写入 `messages` 并重新进入 `generate_plan`。5 分钟未确认会自动视为 `timeout`，并发送当前最终结果。

## RAG 和工具替换

RAG 现在是空实现：

```python
class MockRetriever:
    async def retrieve(self, query: str) -> list[str]:
        return []
```

后续替换 `agent_sentinel.rag.mock_retriever.MockRetriever` 即可接入向量库、ES 或混合检索。

实时工具现在全部返回 Mock 数据，入口是 `agent_sentinel.tools.mock_tools.fetch_all_live_data()`。内部使用 `asyncio.gather` 并发调用：

- `get_metrics`
- `query_logs`
- `get_topology`

每个工具都有 `asyncio.timeout(5)` 和 tenacity 重试。

## 旧接口兼容

这些接口仍然保留：

- `POST /chat/once`
- `POST /alerts/report`
- `GET /alerts/recent`
- `POST /alerts/analyze`
- `POST /feishu/events`

新增接口：

- `POST /aiops/diagnose`: 运行 LangGraph 多 Agent 诊断
- `POST /feishu/card/callback`: 飞书交互卡片回调

## 测试

```powershell
.\.venv\Scripts\pytest.exe
```

`tests/test_workflow.py` 使用 Mock LLM 和禁用人工确认，确保 LangGraph 能编译并执行完整示例流程。
