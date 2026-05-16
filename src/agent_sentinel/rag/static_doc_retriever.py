from __future__ import annotations

import logging
from dataclasses import dataclass

from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.milvus_client import MilvusVectorClient
from agent_sentinel.rag.models import RagFilters, RetrievedDoc

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StaticDocRetriever:
    milvus: MilvusVectorClient
    embedding: EmbeddingClient
    collection_name: str
    top_k: int = 8
    weight: float = 0.55

    async def retrieve(self, query: str, filters: RagFilters | None = None) -> list[RetrievedDoc]:
        query_embedding = await self.embedding.embed(query)
        expr = _build_static_expr(filters)
        docs = await self.milvus.search(
            collection_name=self.collection_name,
            query_embedding=query_embedding,
            top_k=self.top_k,
            source_type="static_doc",
            expr=expr,
        )
        for doc in docs:
            doc.weighted_score = doc.score * self.weight
        logger.info("Static doc RAG completed docs=%s", len(docs))
        return docs


def _build_static_expr(filters: RagFilters | None) -> str | None:
    if not filters or not filters.service:
        return None
    return f'service == "{filters.service}" or service == "global"'
