# Agent Sentinel

Agent Sentinel 是一个基于 FastAPI、LangGraph、LangChain、飞书机器人和 Milvus 的 AIOps 告警诊断系统。当前版本重点支持：

- 飞书群内 @ 机器人后，在原消息话题中使用单张交互卡片动态更新诊断进度。
- LangGraph 多节点诊断流：告警理解（缓存查找） -> RAG检索 -> 实时数据 -> 方案生成 -> 方案校验 -> 人工确认 -> 反馈学习。
- Milvus 静态知识库 `aiops_static_docs` 和历史成功案例库 `aiops_message_history`。
- Markdown 故障手册切片、元数据提取和向量入库。
- 历史成功案例缓存复用，以及用户确认有效后的反馈入库。

## 项目结构

```text
.
├─ config/
│  ├─ settings.yaml
│  ├─ workflow.yaml
│  ├─ .env.example
│  └─ prompts/
├─ data/
│  └─ static_docs/
├─ scripts/
├─ src/
│  └─ agent_sentinel/
│     ├─ agents/
│     ├─ feishu/
│     ├─ graph/
│     ├─ interactive_topic/
│     ├─ rag/
│     └─ tools/
├─ tests/
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

启动服务：

```powershell
.\.venv\Scripts\python.exe -m agent_sentinel.main
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

## Docker / ACR

本地构建：

```bash
docker build -t crpi-zzm4e139q0k3kyai.cn-hangzhou.personal.cr.aliyuncs.com/novamate/agentsentinel:rag .
```

服务器拉取当前镜像：

```bash
docker pull crpi-zzm4e139q0k3kyai.cn-hangzhou.personal.cr.aliyuncs.com/novamate/agentsentinel:rag
```

## 飞书单卡片交互流程

开启 `INTERACTIVE_TOPIC_ENABLED=true` 后，用户在飞书群 @ 机器人，系统会在原消息话题中创建一张“智能诊断工作流”卡片，并持续 PATCH 更新同一张卡片，避免群聊刷屏。

当前卡片步骤：

```text
1. 告警理解（缓存查找）
2. RAG检索
3. 实时数据
4. 方案生成
```

每个节点会显示状态：

- `✅ 已完成`
- `🔄 正在执行`
- `⏸ 等待确认`
- `⏭ 已跳过`
- `❌ 失败`

节点完成后，卡片会出现“同意”和“拒绝”按钮：

- 同意：进入下一节点。
- 拒绝：重试当前节点。
- 重试 2 次后：自动跳过当前节点并进入下一节点。
- 超时未操作：按现有逻辑自动继续。

最终诊断结果会继续显示在同一张卡片上，并增加反馈按钮：

- `✅ 有效，存入知识库`
- `❌ 无效，不存储`

点击“有效”后，本次告警、RAG 结果、工具数据、最终方案、证据链会写入 `aiops_message_history`，作为后续相似告警的历史成功案例。

关键配置：

```env
FEISHU_APP_ID=your-feishu-app-id
FEISHU_APP_SECRET=your-feishu-app-secret
FEISHU_LONG_CONNECTION_ENABLED=true
FEISHU_MESSAGE_POLLING_ENABLED=false
FEISHU_ANALYZE_MENTION_ONLY=true
FEISHU_ALLOWED_CHAT_IDS=oc_xxx
INTERACTIVE_TOPIC_ENABLED=true
INTERACTIVE_TOPIC_WAIT_SECONDS=15
```

飞书开放平台需要启用：

- 机器人能力
- 消息接收事件 `im.message.receive_v1`
- 卡片回调事件 `card.action.triggered`

长连接模式不需要公网事件回调地址。HTTP 回调模式可配置：

```text
https://你的公网域名/webhook/card
```

## LangGraph 诊断流

主诊断流由 `config/workflow.yaml` 配置，当前节点包括：

```yaml
nodes:
  - name: understand
  - name: retrieve
  - name: fetch_live_data
  - name: generate_plan
  - name: validate
  - name: human_confirm
  - name: final_result
  - name: feedback_learning
```

关键路由：

- `understand` 命中历史缓存后，可直接进入 `final_result`，跳过后续节点。
- `retrieve` 会根据告警内容决定是否调用实时工具。
- `validate` 校验失败会回到 `generate_plan`，重试超过逻辑上限后继续。
- `human_confirm` 拒绝会回到 `generate_plan`。
- `final_result` 后进入 `feedback_learning`，由用户决定是否写入历史案例库。

## Milvus 集合

### `aiops_static_docs`

用于存储静态故障手册、SOP、Runbook、FAQ。

### `aiops_message_history`

用于存储历史成功案例，字段包括：

```text
id: VARCHAR(256) primary key
text: VARCHAR(65535)
embedding: FLOAT_VECTOR(dim=EMBEDDING_DIMENSION)
doc_type: VARCHAR(64)
title: VARCHAR(512)
service: VARCHAR(128)
component: VARCHAR(128)
tags: VARCHAR(1024)
version: VARCHAR(64)
updated_at: INT64
created_at: INT64
source_uri: VARCHAR(1024)
source: VARCHAR(1024)
metadata: VARCHAR(8192)
```

历史成功案例统一使用：

```text
doc_type = "alert_case"
```

系统写入 Milvus 前会对 VARCHAR 字段按 UTF-8 字节安全截断，避免中文内容超过 Milvus 字段长度限制。

## RAG 配置

当前默认使用 Top2：

```env
RAG_PROVIDER=milvus
RAG_FINAL_TOP_K=2
RAG_MMR_LAMBDA=0.55

RAG_STATIC_ENABLED=true
RAG_STATIC_COLLECTION=aiops_static_docs
RAG_STATIC_TOP_K=2
RAG_STATIC_WEIGHT=0.55

RAG_MESSAGE_ENABLED=true
RAG_MESSAGE_COLLECTION=aiops_message_history
RAG_MESSAGE_TOP_K=2
RAG_MESSAGE_WEIGHT=0.45
RAG_MESSAGE_DEFAULT_DAYS=30

RAG_CASE_CACHE_ENABLED=true
RAG_CASE_CACHE_THRESHOLD=0.85
RAG_CASE_CACHE_TOP_K=2
RAG_FEEDBACK_ENABLED=true
```

嵌入模型配置示例：

```env
EMBEDDING_API_KEY=your-embedding-api-key
EMBEDDING_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSION=1024
EMBEDDING_MOCK_ENABLED=false
```

说明：

- `RAG_CASE_CACHE_TOP_K`：告警理解（缓存查找）阶段检索历史成功案例 TopK。
- `RAG_MESSAGE_TOP_K`：RAG 检索阶段从历史案例库召回 TopK。
- `RAG_STATIC_TOP_K`：RAG 检索阶段从静态文档库召回 TopK。
- `RAG_FINAL_TOP_K`：Hybrid RAG 合并和 MMR 后的最终 TopK。

在飞书单卡片流程里，`RAG检索` 会分两块展示：

- 文档/混合召回 Top2
- 历史案例召回 Top2

这样历史案例不会被静态文档 Top2 挤掉。

## 静态文档切片与入库

Markdown 故障手册使用 `MarkdownHeaderTextSplitter` 按标题层级切片：

```python
[("#", "h1"), ("##", "h2"), ("###", "h3")]
```

切片后会补充元数据：

- `doc_id`
- `section`
- `alert_category`
- `severity_level`
- `error_code`
- `keywords`
- `last_updated`

初始化集合和上传静态文档：

```powershell
.\.venv\Scripts\python.exe scripts\init_milvus_collections.py --static-only
.\.venv\Scripts\python.exe scripts\ingest_static_docs.py --path data\static_docs
```

服务器容器内检查静态文档：

```bash
docker run --rm --network host \
  crpi-zzm4e139q0k3kyai.cn-hangzhou.personal.cr.aliyuncs.com/novamate/agentsentinel:rag \
  python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://127.0.0.1:19530'); print(c.query(collection_name='aiops_static_docs', filter='', limit=5, output_fields=['id','title','source','text','metadata']))"
```

## 历史案例查询

按 id 查询历史案例：

```bash
docker run --rm --network host \
  crpi-zzm4e139q0k3kyai.cn-hangzhou.personal.cr.aliyuncs.com/novamate/agentsentinel:rag \
  python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://127.0.0.1:19530'); print(c.query(collection_name='aiops_message_history', filter='id == \"alert-case-xxx\"', limit=1, output_fields=['id','title','doc_type','service','component','tags','created_at','updated_at','text','metadata']))"
```

查看集合统计：

```bash
docker run --rm --network host \
  crpi-zzm4e139q0k3kyai.cn-hangzhou.personal.cr.aliyuncs.com/novamate/agentsentinel:rag \
  python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://127.0.0.1:19530'); [print(x, c.get_collection_stats(x)) for x in c.list_collections()]"
```

## HTTP 诊断接口

```powershell
curl -X POST http://127.0.0.1:8000/aiops/diagnose ^
  -H "Content-Type: application/json" ^
  -H "X-Alert-Token: replace-with-a-strong-token" ^
  -d "{\"chat_id\":\"\",\"source\":\"alert-bot\",\"level\":\"ERROR\",\"summary\":\"订单同步超时\",\"details\":\"timeout after 3 retries\",\"raw_text\":\"[ALERT] order sync timeout\",\"trigger_type\":\"bot_alert\",\"tags\":[\"prod\",\"order\"]}"
```

`chat_id` 为空时不会真实发送飞书消息，但会执行 LangGraph 流程。接入飞书群时填入真实 `chat_id`。

## 旧接口兼容

保留接口：

- `POST /chat/once`
- `POST /alerts/report`
- `GET /alerts/recent`
- `POST /alerts/analyze`
- `POST /feishu/events`

新增/核心接口：

- `POST /aiops/diagnose`
- `POST /feishu/card/callback`
- `POST /webhook/card`

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前测试覆盖：

- LangGraph 诊断流
- 飞书事件与卡片回调
- 单卡片交互式流程
- RAG MMR
- Markdown 静态文档切片
- 历史案例缓存命中与反馈入库
- Milvus VARCHAR 字段截断
