from __future__ import annotations

import asyncio

from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.static_ingest import StaticDocIngestor, load_static_documents, split_and_extract_metadata


class FakeMilvus:
    def __init__(self) -> None:
        self.collection: tuple[str, int] | None = None
        self.records: list[dict[str, object]] = []

    async def ensure_static_doc_collection(self, collection_name: str, dimension: int) -> None:
        self.collection = (collection_name, dimension)

    async def upsert(self, collection_name: str, records: list[dict[str, object]]) -> None:
        self.records.extend(records)


def test_load_markdown_static_doc_with_frontmatter(tmp_path) -> None:
    doc_path = tmp_path / "order-sync.md"
    doc_path.write_text(
        """---
id: runbook-order-sync
title: 订单同步排查
service: order-sync
tags:
  - timeout
metadata:
  owner: sre
---

# 订单同步排查

检查下游服务延迟。
""",
        encoding="utf-8",
    )

    docs = load_static_documents(tmp_path)

    assert len(docs) == 1
    assert docs[0].id == "runbook-order-sync"
    assert docs[0].title == "订单同步排查"
    assert docs[0].service == "order-sync"
    assert docs[0].tags == ["timeout"]
    assert docs[0].metadata["owner"] == "sre"


def test_split_fault_manual_extracts_metadata(tmp_path) -> None:
    doc_path = tmp_path / "manual.md"
    doc_path.write_text(
        """# 第一章 消息队列全维度故障

## 1.1 消息大规模堆积故障

**故障定义**：核心业务致命故障，MQ 控制台堆积量暴涨，可能返回 503。

### 1. 消费算力不足类

- 消费者实例数少于分区数。
- 解决方案：扩容消费者并开启批量消费。
""",
        encoding="utf-8",
    )

    docs = split_and_extract_metadata(str(doc_path))

    assert docs
    metadata = next(doc.metadata for doc in docs if "503" in doc.metadata["error_code"])
    assert metadata["doc_id"] == "fault_manual_v1"
    assert metadata["section"].startswith("第一章_消息队列全维度故障_1.1_消息大规模堆积故障")
    assert metadata["alert_category"] == "MQ"
    assert metadata["severity_level"] == "P0"
    assert metadata["error_code"] == ["503"]
    assert "故障定义" in metadata["keywords"]
    assert metadata["last_updated"] == "2026-05-17"


def test_static_doc_ingestor_builds_records() -> None:
    async def run() -> None:
        milvus = FakeMilvus()
        embedding = EmbeddingClient(mock_enabled=True, dimension=4)
        docs = load_static_documents("data/static_docs")
        ingestor = StaticDocIngestor(
            milvus=milvus,  # type: ignore[arg-type]
            embedding=embedding,
            collection_name="aiops_static_docs",
            dimension=4,
            batch_size=2,
        )

        count = await ingestor.ingest_documents(docs)

        assert count >= 1
        assert milvus.collection == ("aiops_static_docs", 4)
        assert milvus.records[0]["id"] == "runbook-order-sync-timeout"
        assert len(milvus.records[0]["embedding"]) == 4

    asyncio.run(run())
