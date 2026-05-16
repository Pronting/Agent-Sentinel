from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class MockRetriever:
    async def retrieve(self, query: str) -> list[str]:
        logger.info("Mock RAG retrieval skipped query_chars=%s", len(query))
        # TODO: Replace with a real vector store or hybrid retrieval backend.
        return []
