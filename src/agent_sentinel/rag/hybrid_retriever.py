from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from agent_sentinel.rag.base import BaseRetriever
from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.mmr import select_mmr
from agent_sentinel.rag.models import RagFilters, RetrievedDoc

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HybridRetriever:
    embedding: EmbeddingClient
    retrievers: list[BaseRetriever] = field(default_factory=list)
    final_top_k: int = 6
    mmr_lambda: float = 0.55

    async def retrieve(self, query: str, filters: RagFilters | None = None) -> list[RetrievedDoc]:
        if not self.retrievers:
            return []

        query_embedding = await self.embedding.embed(query)
        results = await asyncio.gather(
            *(retriever.retrieve(query, filters) for retriever in self.retrievers),
            return_exceptions=True,
        )
        docs: list[RetrievedDoc] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("RAG retriever failed: %s", result)
                continue
            docs.extend(result)

        deduped = _dedupe_docs(docs)
        selected = select_mmr(
            query_embedding=query_embedding,
            docs=deduped,
            top_k=self.final_top_k,
            lambda_mult=self.mmr_lambda,
        )
        logger.info("Hybrid RAG completed candidates=%s selected=%s", len(docs), len(selected))
        return selected


def _dedupe_docs(docs: list[RetrievedDoc]) -> list[RetrievedDoc]:
    by_key: dict[str, RetrievedDoc] = {}
    for doc in docs:
        key = f"{doc.source_type}:{doc.id or doc.text[:80]}"
        existing = by_key.get(key)
        if existing is None or doc.weighted_score > existing.weighted_score:
            by_key[key] = doc
    return list(by_key.values())
