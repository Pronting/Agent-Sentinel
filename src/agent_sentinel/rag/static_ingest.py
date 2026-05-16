from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_sentinel.rag.embedding import EmbeddingClient
from agent_sentinel.rag.milvus_client import MilvusVectorClient

logger = logging.getLogger(__name__)


class StaticDocument(BaseModel):
    id: str
    text: str
    doc_type: str = "runbook"
    title: str
    service: str = "global"
    component: str = ""
    tags: list[str] = Field(default_factory=list)
    version: str = "v1"
    updated_at: int = 0
    source_uri: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class StaticDocIngestor:
    milvus: MilvusVectorClient
    embedding: EmbeddingClient
    collection_name: str
    dimension: int
    batch_size: int = 16

    async def ensure_collection(self) -> None:
        await self.milvus.ensure_static_doc_collection(self.collection_name, self.dimension)

    async def ingest_documents(self, docs: list[StaticDocument]) -> int:
        if not docs:
            return 0
        await self.ensure_collection()

        total = 0
        for start in range(0, len(docs), self.batch_size):
            batch = docs[start : start + self.batch_size]
            records = await asyncio.gather(*(self._to_record(doc) for doc in batch))
            await self.milvus.upsert(self.collection_name, records)
            total += len(records)
            logger.info("Static docs ingested batch=%s total=%s", len(records), total)
        return total

    async def _to_record(self, doc: StaticDocument) -> dict[str, Any]:
        embedding = await self.embedding.embed(doc.text)
        now = int(time.time())
        metadata = {
            **doc.metadata,
            "tags": doc.tags,
        }
        return {
            "id": doc.id,
            "text": doc.text,
            "embedding": embedding,
            "doc_type": doc.doc_type,
            "title": doc.title,
            "service": doc.service,
            "component": doc.component,
            "tags": json.dumps(doc.tags, ensure_ascii=False),
            "version": doc.version,
            "updated_at": doc.updated_at or now,
            "created_at": doc.updated_at or now,
            "source_uri": doc.source_uri,
            "source": doc.source_uri,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }


def load_static_documents(path: str | Path) -> list[StaticDocument]:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"Static docs path does not exist: {root}")
    if root.is_file():
        return _load_file(root)

    docs: list[StaticDocument] = []
    for file_path in sorted(root.rglob("*")):
        if file_path.suffix.lower() not in {".md", ".markdown", ".jsonl"}:
            continue
        docs.extend(_load_file(file_path))
    return docs


def _load_file(path: Path) -> list[StaticDocument]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return [_load_markdown(path)]
    if suffix == ".jsonl":
        return _load_jsonl(path)
    return []


def _load_markdown(path: Path) -> StaticDocument:
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw)
    title = str(frontmatter.get("title") or _extract_markdown_title(body) or path.stem)
    doc_id = str(frontmatter.get("id") or path.stem)
    updated_at = _to_int(frontmatter.get("updated_at"), 0)
    tags = _to_tags(frontmatter.get("tags"))
    metadata = _to_dict(frontmatter.get("metadata"))
    metadata.setdefault("file_path", str(path))
    return StaticDocument(
        id=doc_id,
        text=body.strip(),
        doc_type=str(frontmatter.get("doc_type") or "runbook"),
        title=title,
        service=str(frontmatter.get("service") or "global"),
        component=str(frontmatter.get("component") or ""),
        tags=tags,
        version=str(frontmatter.get("version") or "v1"),
        updated_at=updated_at,
        source_uri=str(frontmatter.get("source_uri") or path.as_posix()),
        metadata=metadata,
    )


def _load_jsonl(path: Path) -> list[StaticDocument]:
    docs: list[StaticDocument] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        payload.setdefault("id", f"{path.stem}-{line_number}")
        payload.setdefault("title", payload["id"])
        payload.setdefault("metadata", {})
        payload["metadata"]["file_path"] = str(path)
        payload["metadata"]["line_number"] = line_number
        docs.append(StaticDocument.model_validate(payload))
    return docs


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    metadata = yaml.safe_load(parts[1]) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, parts[2]


def _extract_markdown_title(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _to_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _to_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
