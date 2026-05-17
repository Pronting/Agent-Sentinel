from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.milvus_client import MilvusVectorClient
from agent_sentinel.rag.models import RagFilters, RetrievedDoc

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MessageHistoryRetriever:
    milvus: MilvusVectorClient
    embedding: EmbeddingClient
    collection_name: str
    top_k: int = 12
    weight: float = 0.45
    default_days: int = 30

    async def retrieve(self, query: str, filters: RagFilters | None = None) -> list[RetrievedDoc]:
        query_embedding = await self.embedding.embed(query)
        expr = self._build_message_expr(filters)
        docs = await self.milvus.search(
            collection_name=self.collection_name,
            query_embedding=query_embedding,
            top_k=self.top_k,
            source_type="message_history",
            expr=expr,
        )
        now = int(time.time())
        for doc in docs:
            recency_boost = self._recency_boost(now, doc.created_at)
            doc.weighted_score = doc.score * self.weight * recency_boost
        logger.info("Message history RAG completed docs=%s", len(docs))
        return docs

    def _build_message_expr(self, filters: RagFilters | None) -> str | None:
        clauses: list[str] = ['doc_type == "alert_case"']
        if self.default_days > 0:
            min_created_at = int(time.time()) - self.default_days * 86400
            clauses.append(f"created_at >= {min_created_at}")
        return " and ".join(clauses) if clauses else None

    def _recency_boost(self, now: int, created_at: int | None) -> float:
        if not created_at:
            return 1.0
        age_days = max((now - created_at) / 86400, 0)
        if age_days <= 7:
            return 1.2
        if age_days <= 30:
            return 1.0
        return 0.8
