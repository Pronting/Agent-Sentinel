from __future__ import annotations

import argparse
import asyncio
import logging

from agent_sentinel.config import get_settings
from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.milvus_client import MilvusSearchConfig, MilvusVectorClient
from agent_sentinel.rag.static_ingest import StaticDocIngestor, load_static_documents


async def main() -> int:
    parser = argparse.ArgumentParser(description="Upload static RAG documents into Milvus.")
    parser.add_argument(
        "--path",
        default="data/static_docs",
        help="Markdown file, JSONL file, or directory containing static docs.",
    )
    parser.add_argument("--collection", default=None, help="Override target Milvus collection.")
    parser.add_argument("--batch-size", type=int, default=16, help="Upsert batch size.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    settings = get_settings()
    if not settings.milvus_uri:
        raise SystemExit("MILVUS_URI is required.")

    docs = load_static_documents(args.path)
    logging.info("Loaded static docs count=%s path=%s", len(docs), args.path)
    if not docs:
        return 0

    milvus = MilvusVectorClient(
        MilvusSearchConfig(
            uri=settings.milvus_uri,
            token=settings.milvus_token,
            user=settings.milvus_user,
            password=settings.milvus_password,
            db_name=settings.milvus_db_name,
        )
    )
    embedding = EmbeddingClient(
        api_key=settings.embedding_api_key or "",
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        mock_enabled=settings.embedding_mock_enabled,
    )
    ingestor = StaticDocIngestor(
        milvus=milvus,
        embedding=embedding,
        collection_name=args.collection or settings.rag_static_collection,
        dimension=settings.embedding_dimension,
        batch_size=args.batch_size,
    )
    count = await ingestor.ingest_documents(docs)
    logging.info("Static docs upload completed count=%s", count)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
