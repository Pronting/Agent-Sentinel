from __future__ import annotations

import argparse
import asyncio
import logging

from agent_sentinel.config import get_settings
from agent_sentinel.rag.milvus_client import MilvusSearchConfig, MilvusVectorClient


async def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize Milvus collections for Agent Sentinel RAG.")
    parser.add_argument("--static-only", action="store_true", help="Initialize only static doc collection.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    settings = get_settings()
    if not settings.milvus_uri:
        raise SystemExit("MILVUS_URI is required.")

    milvus = MilvusVectorClient(
        MilvusSearchConfig(
            uri=settings.milvus_uri,
            token=settings.milvus_token,
            user=settings.milvus_user,
            password=settings.milvus_password,
            db_name=settings.milvus_db_name,
        )
    )
    await milvus.ensure_static_doc_collection(settings.rag_static_collection, settings.embedding_dimension)
    if not args.static_only:
        # Message history uses the same base schema for now; the dynamic fields hold message-specific metadata.
        await milvus.ensure_static_doc_collection(settings.rag_message_collection, settings.embedding_dimension)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
