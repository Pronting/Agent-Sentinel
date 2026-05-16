from __future__ import annotations

import asyncio

from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.static_ingest import StaticDocIngestor, load_static_documents


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
