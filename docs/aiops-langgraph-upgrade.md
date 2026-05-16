# Agent Sentinel LangGraph 多 Agent 告警诊断升级文档

## 1. 背景

本次升级将原有“飞书消息接入 -> 单次 LLM 分析 -> 飞书回复”的线性流程，扩展为基于 LangChain + LangGraph 的多 Agent 智能告警诊断系统。

升级目标是让告警诊断具备以下能力：

- 多步骤状态机编排
- 节点级异步执行
- 节点超时与重试
- Mock RAG 检索预留
- 指标、日志、拓扑工具并发调用
- Prompt 模板外部化
- 统一 LLM 调用封装
- 多模型降级
- 诊断证据链
- 飞书交互卡片人工确认
- 流式推送中间状态
- Docker Compose 运行环境

本次实现保留了原有接口，新增 AIOps 诊断接口，不破坏已有飞书接收、告警上报和消息发送逻辑。

## 2. 改动范围

### 2.1 新增目录

```text
config/
├─ settings.yaml
├─ workflow.yaml
├─ .env.example
└─ prompts/
   ├─ understand.yaml
   ├─ generate_plan.yaml
   └─ validate.yaml

docs/
└─ aiops-langgraph-upgrade.md

src/
├─ main.py
└─ agent_sentinel/
   ├─ agents/
   ├─ feishu/
   ├─ graph/
   ├─ llm/
   ├─ rag/
   ├─ tools/
   └─ utils/
```

### 2.2 新增运行文件

```text
Dockerfile
docker-compose.yml
requirements.txt
```

### 2.3 修改原有文件

```text
.env.example
README.md
pyproject.toml
src/agent_sentinel/config.py
src/agent_sentinel/feishu_app.py
src/agent_sentinel/main.py
```

### 2.4 结构调整

原来的单文件 `src/agent_sentinel/llm.py` 已升级为包结构：

```text
src/agent_sentinel/llm/
├─ __init__.py
├─ client.py
└─ executor.py
```

`__init__.py` 中保留了原来的 `build_chat_model` 导出，因此旧代码中的：

```python
from agent_sentinel.llm import build_chat_model
```

仍然可用。

## 3. 新系统架构

### 3.1 总体流程

```text
飞书消息 / HTTP 告警
        |
        v
FastAPI 接口
        |
        v
LangGraph DiagnosisWorkflow
        |
        v
understand
        |
        v
retrieve
        |
        v
should_fetch 条件路由
        |
        +---- fetch_live_data
        |          |
        |          v
        +---- generate_plan
                   |
                   v
                validate
                   |
                   v
             human_confirm
                   |
                   v
              final_result
```

### 3.2 关键模块职责

| 模块 | 职责 |
| --- | --- |
| `agents/understand_agent.py` | 使用外部 Prompt 调用 LLM，生成告警摘要 |
| `agents/retrieve_agent.py` | Mock RAG 检索，当前返回空列表 |
| `agents/fetch_tools.py` | 并发调用 Mock 指标、日志、拓扑工具 |
| `agents/generate_plan.py` | 生成结构化诊断方案和证据链 |
| `agents/validate_plan.py` | 规则校验 + 轻量 LLM 校验 |
| `agents/human_confirm.py` | 发送飞书交互卡片并等待确认 |
| `agents/final_result.py` | 组装最终诊断结果并发送飞书 |
| `graph/workflow.py` | 读取 YAML 动态构建 LangGraph |
| `llm/executor.py` | 统一 LLM 调用、超时、重试、多模型降级、日志 |
| `tools/mock_tools.py` | Mock 指标、日志、拓扑工具，使用 `asyncio.gather` 并发 |
| `rag/mock_retriever.py` | RAG 空实现，预留真实检索替换点 |
| `feishu/sender.py` | 飞书文本和交互卡片发送封装 |
| `feishu/card_handler.py` | 飞书卡片回调处理和人工决策保存 |

## 4. 状态模型

状态定义位于：

```text
src/agent_sentinel/graph/state.py
```

核心状态如下：

```python
class DiagnosisState(TypedDict, total=False):
    raw_alert: dict[str, Any]
    alert_summary: str
    retrieved_docs: list[str]
    live_data: dict[str, Any]
    recommended_plan: dict[str, Any]
    evidence: list[str]
    validation_result: bool
    need_human: bool
    messages: list[dict[str, str]]
    chat_id: str
    thread_root_message_id: str | None
    mention_open_id: str | None
    mention_name: str | None
    decision_id: str
    human_decision: str
    validation_attempts: int
    final_text: str
```

其中 `evidence` 是本次升级新增的诊断证据链字段。各节点会把自己的判断依据追加进去，最终结果会把证据链一起输出。

## 5. LangGraph Workflow 配置化

工作流配置位于：

```text
config/workflow.yaml
```

当前节点：

```yaml
nodes:
  - name: understand
  - name: retrieve
  - name: fetch_live_data
  - name: generate_plan
  - name: validate
  - name: human_confirm
  - name: final_result
```

普通边：

```yaml
edges:
  - from: understand
    to: retrieve
```

条件边：

```yaml
conditional_edges:
  - from: retrieve
    condition: should_fetch
    mapping:
      fetch: fetch_live_data
      skip: generate_plan
```

实际构图逻辑在：

```text
src/agent_sentinel/graph/workflow.py
```

`DiagnosisWorkflow.compile()` 会读取 YAML，并把节点名称映射到 `_node_registry()` 中注册的异步函数。

## 6. Agent 节点说明

### 6.1 understand

文件：

```text
src/agent_sentinel/agents/understand_agent.py
```

能力：

- 从 `config/prompts/understand.yaml` 读取 Prompt
- 调用 `LLMExecutor`
- 使用 `asyncio.timeout(10)` 控制节点超时
- 使用 tenacity 最多重试 2 次
- 写入 `alert_summary`
- 追加证据链

### 6.2 retrieve

文件：

```text
src/agent_sentinel/agents/retrieve_agent.py
src/agent_sentinel/rag/mock_retriever.py
```

当前实现：

```python
async def retrieve(self, query: str) -> list[str]:
    return []
```

这是 RAG 空实现，后续可以替换为向量库、Elasticsearch、OpenSearch 或混合检索。

### 6.3 should_fetch 条件路由

根据告警摘要和原始告警内容判断是否需要实时数据。

当前关键词包括：

```text
error
critical
timeout
latency
失败
超时
异常
错误
```

返回值：

- `fetch`: 进入 `fetch_live_data`
- `skip`: 跳过实时工具，直接进入 `generate_plan`

### 6.4 fetch_live_data

文件：

```text
src/agent_sentinel/tools/mock_tools.py
```

包含三个 Mock 工具：

- `get_metrics`
- `query_logs`
- `get_topology`

并发执行方式：

```python
metrics, logs, topology = await asyncio.gather(
    get_metrics(alert_summary),
    query_logs(alert_summary),
    get_topology(alert_summary),
)
```

每个工具都有：

- `asyncio.timeout(5)`
- tenacity 最多重试 2 次
- 详细日志

### 6.5 generate_plan

文件：

```text
src/agent_sentinel/agents/generate_plan.py
```

能力：

- 从 `config/prompts/generate_plan.yaml` 加载 Prompt
- 使用 `PydanticOutputParser` 解析结构化输出
- 输出 `recommended_plan`
- 输出 `evidence`
- 输出 `need_human`

结构化输出模型：

```python
class PlanOutput(BaseModel):
    recommended_plan: dict[str, Any]
    evidence: list[str]
    need_human: bool
```

### 6.6 validate

文件：

```text
src/agent_sentinel/agents/validate_plan.py
```

校验分两层：

- 规则校验：拦截危险命令
- LLM 校验：输出 `PASS` 或 `FAIL`

当前危险关键词：

```text
rm -rf
drop table
shutdown
reboot
kubectl delete
delete from
```

校验失败时会回到 `generate_plan`。当前最多允许一次失败重试，避免工作流无限循环。

### 6.7 human_confirm

文件：

```text
src/agent_sentinel/agents/human_confirm.py
src/agent_sentinel/feishu/sender.py
src/agent_sentinel/feishu/card_handler.py
```

能力：

- 发送飞书交互卡片
- 卡片包含“同意”和“拒绝”按钮
- 等待用户回调
- 默认 5 分钟超时
- 超时后自动返回 `timeout`
- 拒绝时把反馈写入 `messages`，重新进入 `generate_plan`

### 6.8 final_result

文件：

```text
src/agent_sentinel/agents/final_result.py
```

能力：

- 组装最终诊断文本
- 包含告警摘要、推荐方案、校验结果、人工决策和证据链
- 发送到飞书群

## 7. 统一 LLM 调用封装

文件：

```text
src/agent_sentinel/llm/executor.py
```

`LLMExecutor` 支持：

- 多模型列表
- 主备降级
- 调用超时
- 调用重试
- 异步调用
- Mock 模式
- 日志记录

初始化参数：

```python
LLMExecutor(
    models=["gpt-4o-mini", "gpt-3.5-turbo"],
    api_key="...",
    base_url="...",
    temperature=0.0,
    timeout_seconds=15,
    max_retries=2,
    mock_enabled=False,
)
```

降级逻辑：

1. 优先调用第一个模型
2. 如果失败，记录异常
3. 自动切换到下一个模型
4. 所有模型失败后抛出异常

Mock 模式适合本地演示和测试：

```env
AIOPS_MOCK_LLM_ENABLED=true
```

## 8. 飞书交互卡片

### 8.1 卡片发送

文件：

```text
src/agent_sentinel/feishu/sender.py
```

发送方法：

```python
await sender.send_card(
    chat_id,
    decision_id,
    plan,
    evidence,
    thread_root_message_id=thread_root_message_id,
)
```

卡片包含：

- 推荐方案
- 证据链
- 同意按钮
- 拒绝按钮

### 8.2 回调接口

新增接口：

```text
POST /feishu/card/callback
```

飞书后台交互卡片回调地址配置为：

```text
https://你的域名/feishu/card/callback
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

拒绝示例：

```json
{
  "action": {
    "value": {
      "action": "diagnosis_confirm",
      "decision_id": "uuid",
      "decision": "rejected",
      "feedback": "需要补充数据库连接池状态"
    }
  }
}
```

### 8.3 决策保存

`HumanDecisionStore` 支持内存 Future 和 Redis 两种方式。

Redis 配置：

```env
REDIS_URL=redis://localhost:6379/0
```

当前 Redis 用于保存人工确认结果。后续如果要做完整工作流恢复，可以进一步接入 LangGraph checkpoint。

## 9. 流式中间状态

`DiagnosisWorkflow.run_streaming()` 使用：

```python
async for update in app.astream(...):
    ...
```

每个节点完成后，系统会尝试发送飞书中间状态：

```text
✅ 已完成告警理解，正在检索历史案例...
✅ 历史案例检索完成，正在判断是否需要实时数据...
✅ 实时指标、日志、拓扑已获取，正在生成诊断方案...
✅ 诊断方案已生成，正在进行安全校验...
✅ 方案校验完成，等待人工确认...
✅ 人工确认流程结束，正在发送最终结果...
✅ 最终诊断结果已发送。
```

如果 `chat_id` 为空，或者飞书配置不完整，发送会被跳过，但工作流仍会正常执行。

## 10. 新增接口

### 10.1 AIOps 诊断接口

```text
POST /aiops/diagnose
```

请求示例：

```bash
curl -X POST http://127.0.0.1:8000/aiops/diagnose \
  -H "Content-Type: application/json" \
  -H "X-Alert-Token: replace-with-a-strong-token" \
  -d '{
    "chat_id": "",
    "source": "alert-bot",
    "level": "ERROR",
    "summary": "订单同步超时",
    "details": "timeout after 3 retries",
    "raw_text": "[ALERT] order sync timeout",
    "trigger_type": "bot_alert",
    "tags": ["prod", "order"]
  }'
```

响应字段：

```json
{
  "status": "ok",
  "summary": "...",
  "recommended_plan": {},
  "evidence": [],
  "validation_result": true,
  "human_decision": "approved",
  "final_text": "..."
}
```

### 10.2 飞书卡片回调接口

```text
POST /feishu/card/callback
```

用于接收“同意”或“拒绝”的交互卡片回调。

## 11. 保留的旧接口

以下旧接口仍然可用：

```text
GET  /health
POST /chat/once
POST /alerts/test
POST /alerts/report
GET  /alerts/recent
POST /alerts/analyze
POST /feishu/events
```

其中 `/alerts/analyze` 仍走原来的简单告警分析流程，`/aiops/diagnose` 走新的 LangGraph 多 Agent 流程。

## 12. 配置说明

### 12.1 `.env`

建议从模板生成：

```powershell
copy config\.env.example .env
```

关键配置：

```env
OPENAI_API_KEY=your-openai-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0
OPENAI_HTTP_TRUST_ENV=false

AIOPS_SETTINGS_PATH=config/settings.yaml
AIOPS_WORKFLOW_CONFIG_PATH=config/workflow.yaml
AIOPS_LLM_MODELS=gpt-4o-mini,gpt-3.5-turbo
AIOPS_MOCK_LLM_ENABLED=true
AIOPS_HUMAN_CONFIRM_ENABLED=false
AIOPS_HUMAN_CONFIRM_TIMEOUT_SECONDS=300
REDIS_URL=redis://localhost:6379/0
```

### 12.2 `config/settings.yaml`

用于默认配置：

```yaml
llm:
  models:
    - gpt-4o-mini
    - gpt-3.5-turbo
  timeout_seconds: 15
  max_retries: 2
  mock_enabled: true

workflow:
  config_path: config/workflow.yaml
  human_confirm_timeout_seconds: 300
  human_confirm_enabled: true
```

`.env` 中的同名配置优先生效。

## 13. 本地运行步骤

```powershell
cd E:\pythonStudy\code\Agent-Sentinel
.\.venv\Scripts\activate
pip install -e .[dev]
copy config\.env.example .env
```

本地先跑 Mock 流程：

```env
AIOPS_MOCK_LLM_ENABLED=true
AIOPS_HUMAN_CONFIRM_ENABLED=false
```

启动：

```powershell
.\.venv\Scripts\python.exe -m agent_sentinel.main
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

执行 AIOps 诊断：

```powershell
curl -X POST http://127.0.0.1:8000/aiops/diagnose ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: replace-with-a-strong-token" ^
  -d "{\"chat_id\":\"\",\"source\":\"alert-bot\",\"level\":\"ERROR\",\"summary\":\"订单同步超时\",\"details\":\"timeout after 3 retries\",\"raw_text\":\"[ALERT] order sync timeout\",\"trigger_type\":\"bot_alert\",\"tags\":[\"prod\",\"order\"]}"
```

## 14. Docker 运行

```powershell
copy config\.env.example .env
docker compose up --build
```

服务端口：

```text
http://127.0.0.1:8000
```

Compose 包含：

- `app`: FastAPI 服务
- `redis`: 人工确认结果缓存

## 15. 测试验证

测试命令：

```powershell
.\.venv\Scripts\pytest.exe
```

当前验证结果：

```text
6 passed
```

覆盖内容：

- 健康检查
- 告警上报
- 飞书 challenge
- 原有 `/alerts/analyze` 兼容
- 飞书线程回复兼容
- 新 LangGraph Mock 工作流可编译并执行

## 16. 后续替换建议

### 16.1 替换真实 RAG

替换位置：

```text
src/agent_sentinel/rag/mock_retriever.py
```

建议接口保持不变：

```python
async def retrieve(self, query: str) -> list[str]:
    ...
```

可接入：

- FAISS
- Milvus
- Elasticsearch
- OpenSearch
- pgvector
- 企业知识库 API

### 16.2 替换真实工具

替换位置：

```text
src/agent_sentinel/tools/mock_tools.py
```

建议保持三个工具边界：

- 指标：Prometheus、Grafana、云监控
- 日志：Loki、ELK、SLS
- 拓扑：CMDB、K8s、服务治理平台

保留 `asyncio.gather` 并发执行，避免诊断链路被单个工具拖慢。

### 16.3 增强工作流持久化

当前人工确认使用 Redis 保存决策结果。后续如果需要完整恢复中断工作流，可以接入 LangGraph checkpointer。

建议方向：

- Redis checkpoint
- Postgres checkpoint
- LangGraph thread_id
- 飞书 message_id 与 workflow run_id 绑定

### 16.4 增强卡片交互

后续可扩展按钮：

- 同意并执行
- 仅记录建议
- 重新分析
- 补充上下文
- 转人工值班

## 17. 风险与注意事项

- 当前 RAG 是空实现，不会返回历史案例。
- 当前工具全部是 Mock 数据，不会访问真实监控系统。
- `AIOPS_HUMAN_CONFIRM_ENABLED=true` 且飞书未配置时，节点会跳过真实发送，但仍可能等待确认；本地演示建议关闭人工确认。
- 飞书加密事件当前仍沿用原项目限制，未实现事件解密。
- FastAPI `on_event` 目前有弃用警告，功能不受影响，后续可迁移到 lifespan。

## 18. 文件索引

| 文件 | 说明 |
| --- | --- |
| `src/agent_sentinel/main.py` | FastAPI 主入口，新增 `/aiops/diagnose` 和 `/feishu/card/callback` |
| `src/agent_sentinel/graph/workflow.py` | LangGraph 动态构建和流式执行 |
| `src/agent_sentinel/graph/state.py` | 诊断状态定义 |
| `src/agent_sentinel/llm/executor.py` | 统一 LLM 调用封装 |
| `src/agent_sentinel/agents/*.py` | 各 Agent 节点 |
| `src/agent_sentinel/tools/mock_tools.py` | Mock 并发工具 |
| `src/agent_sentinel/rag/mock_retriever.py` | Mock RAG |
| `src/agent_sentinel/feishu/sender.py` | 飞书文本和卡片发送 |
| `src/agent_sentinel/feishu/card_handler.py` | 飞书卡片回调处理 |
| `config/workflow.yaml` | 工作流拓扑 |
| `config/prompts/*.yaml` | 节点 Prompt 模板 |
| `tests/test_workflow.py` | LangGraph 工作流测试 |

