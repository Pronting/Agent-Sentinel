from __future__ import annotations

import logging

from agent_sentinel.rag.models import RagFilters, RetrievedDoc

logger = logging.getLogger(__name__)


class MockRetriever:
    async def retrieve(self, query: str, filters: RagFilters | None = None) -> list[RetrievedDoc]:
        logger.info("Mock RAG retrieval skipped query_chars=%s", len(query))
        # TODO: Replace with a real vector store or hybrid retrieval backend.
        return []
