# 双 RAG + Milvus + MMR 设计文档

## 1. 背景

本次新增了一条初步的 RAG 检索链路，用于增强 AIOps 告警诊断中的历史上下文和标准知识引用能力。

系统现在支持两类 RAG 数据源：

- 固定文本 RAG：SOP、Runbook、FAQ、架构说明、故障预案等稳定知识。
- 历史消息 RAG：飞书群消息、历史告警、历史诊断结果、发布变更上下文等动态信息。

两个 RAG 通道会并发召回候选结果，再统一加权、去重，并通过 MMR 选择最终进入 Prompt 的上下文。

## 2. 总体链路

```text
当前告警摘要 alert_summary
        |
        v
Embedding
        |
        +---------------------------+
        |                           |
        v                           v
固定文本 RAG                  历史消息 RAG
aiops_static_docs             aiops_message_history
        |                           |
        +-------------+-------------+
                      |
                      v
              合并 + 加权 + 去重
                      |
                      v
                    MMR
                      |
                      v
              retrieved_docs
                      |
                      v
              generate_plan 节点
```

代码入口在：

```text
src/agent_sentinel/rag/factory.py
```

工作流中的 `retrieve` 节点会调用统一 retriever：

```text
src/agent_sentinel/agents/retrieve_agent.py
```

## 3. 设计原则

### 3.1 固定文本和历史消息分开存

固定文本和历史消息生命周期不同：

- 固定文本更新频率低，可信度高，适合长期保存。
- 历史消息更新频率高，噪声更大，适合按时间窗口检索。

因此建议使用两个 Milvus collection，而不是把所有数据混在一起。

### 3.2 RAG 失败不阻断诊断

RAG 是增强能力，不应该成为告警诊断主流程的单点故障。

当前行为：

- `RAG_PROVIDER=mock` 时不访问 Milvus。
- `RAG_PROVIDER=milvus` 但 `MILVUS_URI` 为空时，自动降级为 Mock RAG。
- 某个 retriever 异常时，Hybrid 层会记录日志并继续使用其他 retriever 的结果。

### 3.3 敏感信息只放 `.env`

Milvus token、账号密码、Embedding API key 等敏感信息不写入代码和 YAML 默认配置。

这些信息通过 `.env` 配置：

```env
MILVUS_TOKEN=
MILVUS_USER=
MILVUS_PASSWORD=
EMBEDDING_API_KEY=
```

## 4. 模块说明

| 文件 | 说明 |
| --- | --- |
| `src/agent_sentinel/rag/base.py` | 定义统一 retriever 协议 |
| `src/agent_sentinel/rag/models.py` | 定义 `RetrievedDoc` 和 `RagFilters` |
| `src/agent_sentinel/rag/embedding.py` | Embedding 调用封装，支持 Mock embedding |
| `src/agent_sentinel/rag/milvus_client.py` | Milvus search 封装 |
| `src/agent_sentinel/rag/static_doc_retriever.py` | 固定文本 RAG |
| `src/agent_sentinel/rag/message_history_retriever.py` | 历史消息 RAG |
| `src/agent_sentinel/rag/hybrid_retriever.py` | 并发召回、加权、去重、MMR |
| `src/agent_sentinel/rag/mmr.py` | MMR 算法 |
| `src/agent_sentinel/rag/factory.py` | 根据配置创建 Mock 或 Milvus RAG |
| `tests/test_rag_mmr.py` | MMR 和降级逻辑测试 |

## 5. 数据模型

### 5.1 统一返回模型

无论来自固定文本还是历史消息，最终都会转为统一结构：

```python
class RetrievedDoc(BaseModel):
    id: str
    text: str
    source_type: SourceType
    score: float = 0.0
    weighted_score: float = 0.0
    service: str | None = None
    title: str | None = None
    created_at: int | None = None
    source_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list, exclude=True)
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `id` | 文档或消息唯一 ID |
| `text` | 进入 RAG 和 Prompt 的正文 |
| `source_type` | 来源类型：`static_doc`、`message_history`、`mock` |
| `score` | Milvus 原始相似度分数 |
| `weighted_score` | 按来源权重调整后的分数 |
| `service` | 关联服务名 |
| `title` | 文档标题或消息标题 |
| `created_at` | 创建时间，Unix timestamp |
| `source_uri` | 文档链接、消息链接或来源地址 |
| `metadata` | 扩展信息 |
| `embedding` | 候选文档向量，用于 MMR 去冗余，不进入 Prompt |

## 6. 固定文本 RAG

### 6.1 Collection 名称

默认：

```text
aiops_static_docs
```

配置项：

```env
RAG_STATIC_COLLECTION=aiops_static_docs
```

### 6.2 建议字段

```text
id: string
text: string
embedding: float_vector
doc_type: string
title: string
service: string
component: string
tags: string
version: string
updated_at: int64
source_uri: string
metadata: string
```

### 6.3 用途

固定文本 RAG 用来回答“标准应该怎么处理”。

适合存储：

- 排障 SOP
- Runbook
- FAQ
- 服务架构说明
- 故障预案
- 值班手册

### 6.4 当前过滤逻辑

当前固定文本 RAG 会根据 `service` 做简单过滤：

```text
service == 当前服务 or service == "global"
```

这样可以同时召回服务专属文档和全局通用文档。

## 7. 历史消息 RAG

### 7.1 Collection 名称

默认：

```text
aiops_message_history
```

配置项：

```env
RAG_MESSAGE_COLLECTION=aiops_message_history
```

### 7.2 建议字段

```text
id: string
text: string
embedding: float_vector
message_id: string
chat_id: string
sender_id: string
sender_name: string
message_type: string
service: string
level: string
tags: string
created_at: int64
thread_root_id: string
source: string
metadata: string
```

### 7.3 用途

历史消息 RAG 用来回答“最近实际发生了什么”。

适合存储：

- 飞书群历史消息
- 告警机器人消息
- 人工排障讨论
- AI 诊断结果
- 发布和变更通知
- 值班交接信息

### 7.4 当前过滤逻辑

当前历史消息 RAG 支持：

- 按 `chat_id` 过滤
- 按最近 N 天过滤

默认时间窗口：

```env
RAG_MESSAGE_DEFAULT_DAYS=30
```

如果 `created_at` 在最近 7 天内，会有轻微时间权重加成。

## 8. MMR 逻辑

MMR 全称是 Maximal Marginal Relevance，用于在“相关性”和“多样性”之间取平衡。

普通 top-k 的问题是容易召回多条内容相似的结果，例如 5 条都在说同一个 timeout。

MMR 的目标是：

- 保留最相关的结果
- 避免结果之间高度重复
- 让 Prompt 里的上下文覆盖更多排障角度

当前公式：

```text
mmr_score = lambda * relevance - (1 - lambda) * diversity_penalty
```

配置：

```env
RAG_MMR_LAMBDA=0.55
```

含义：

- 越接近 `1.0`，越偏相关性。
- 越接近 `0.0`，越偏多样性。

最终返回条数：

```env
RAG_FINAL_TOP_K=6
```

## 9. 配置说明

### 9.1 默认配置

默认配置在：

```text
config/settings.yaml
```

```yaml
rag:
  provider: mock
  final_top_k: 6
  mmr_lambda: 0.55
  static_docs:
    enabled: true
    collection: aiops_static_docs
    top_k: 8
    weight: 0.55
  message_history:
    enabled: true
    collection: aiops_message_history
    top_k: 12
    weight: 0.45
    default_days: 30

embedding:
  model: text-embedding-3-small
  dimension: 1536
  mock_enabled: false
```

### 9.2 `.env` 配置

本地启用 Milvus：

```env
RAG_PROVIDER=milvus
MILVUS_URI=http://localhost:19530
```

Embedding 配置：

```env
EMBEDDING_API_KEY=your-embedding-api-key
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSION=1536
EMBEDDING_MOCK_ENABLED=false
```

Milvus 鉴权配置：

```env
MILVUS_TOKEN=
MILVUS_USER=
MILVUS_PASSWORD=
MILVUS_DB_NAME=
```

固定文本 RAG：

```env
RAG_STATIC_ENABLED=true
RAG_STATIC_COLLECTION=aiops_static_docs
RAG_STATIC_TOP_K=8
RAG_STATIC_WEIGHT=0.55
```

历史消息 RAG：

```env
RAG_MESSAGE_ENABLED=true
RAG_MESSAGE_COLLECTION=aiops_message_history
RAG_MESSAGE_TOP_K=12
RAG_MESSAGE_WEIGHT=0.45
RAG_MESSAGE_DEFAULT_DAYS=30
```

## 10. Docker Compose

`docker-compose.yml` 已加入 Milvus standalone 依赖：

```text
etcd
minio
milvus
redis
app
```

Milvus 对外端口：

```text
19530
9091
```

启动：

```powershell
docker compose up --build
```

应用容器内会使用：

```env
MILVUS_URI=http://milvus:19530
```

## 11. 工作流接入点

`DiagnosisWorkflow` 初始化时会创建 retriever：

```python
self.retriever = retriever or build_retriever(settings)
```

位置：

```text
src/agent_sentinel/graph/workflow.py
```

`retrieve` 节点调用：

```python
docs = await retriever.retrieve(state.get("alert_summary", ""), filters)
```

然后把统一文档格式转为 Prompt 文本：

```python
prompt_docs = [doc.to_prompt_text() for doc in docs]
```

最终写入：

```python
state["retrieved_docs"]
```

`generate_plan` 节点会把这些文档作为历史上下文输入给 LLM。

## 12. 降级行为

### 12.1 默认 Mock

默认配置：

```env
RAG_PROVIDER=mock
```

此时不会访问 Milvus，也不会调用 embedding。

### 12.2 Milvus 未配置

如果设置：

```env
RAG_PROVIDER=milvus
```

但没有配置：

```env
MILVUS_URI
```

系统会自动降级到 Mock RAG。

### 12.3 单路失败

如果固定文本或历史消息某一路失败，Hybrid retriever 会记录日志，并继续使用另一路结果。

## 13. 测试

新增测试：

```text
tests/test_rag_mmr.py
```

覆盖：

- MMR 能在相关性和多样性之间取舍
- `RAG_PROVIDER=milvus` 但缺少 `MILVUS_URI` 时自动降级 Mock

运行：

```powershell
.\.venv\Scripts\pytest.exe tests\test_rag_mmr.py tests\test_workflow.py
```

当前结果：

```text
3 passed
```

完整测试结果：

```text
8 passed
```

## 14. 当前未实现内容

当前已实现固定文本上传基础流程：

```text
data/static_docs/*.md 或 *.jsonl
        |
        v
scripts/ingest_static_docs.py
        |
        v
解析 StaticDocument
        |
        v
Embedding
        |
        v
Milvus upsert
```

初始化 collection：

```powershell
.\.venv\Scripts\python.exe scripts\init_milvus_collections.py --static-only
```

上传固定文本：

```powershell
.\.venv\Scripts\python.exe scripts\ingest_static_docs.py --path data\static_docs
```

Markdown 文档示例：

```markdown
---
id: runbook-order-sync-timeout
title: 订单同步超时排查 SOP
doc_type: runbook
service: order-sync
component: payment-api
tags:
  - timeout
  - order
version: v1
source_uri: data/static_docs/order-sync-timeout.md
metadata:
  owner: sre
---

# 订单同步超时排查 SOP

正文内容...
```

JSONL 文档示例：

```json
{"id":"runbook-001","title":"订单同步超时排查 SOP","service":"order-sync","text":"排查步骤...","tags":["timeout","order"]}
```

以下内容还未实现：

- 历史消息导入脚本
- 飞书消息增量同步到 Milvus
- 文本切分策略
- metadata 更复杂过滤
- reranker
- LangGraph checkpoint 与 RAG 查询结果持久化

## 15. 下一步建议

### 15.1 增加历史消息导入脚本

建议新增：

```text
scripts/ingest_message_history.py
```

初版可以复用已有飞书消息轮询逻辑，把消息清洗后写入 Milvus。

### 15.2 增加检索调试接口

建议新增：

```text
POST /rag/search
```

用于调试当前 query 能召回哪些固定文本和历史消息，方便调权重和 MMR 参数。
