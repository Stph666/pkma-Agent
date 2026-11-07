"""Chroma 向量库封装：写入、按文档删除、带过滤的相似度检索。

约定（实施方案 5.2）：
- 向量 ID 与 document_chunks.id 一致；
- 元数据至少含 document_id、chunk_index、filename、page_start、page_end、category；
- 集合名固定，向量模型变更时启用新集合。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_client = None
_collection = None


@dataclass
class VectorRecord:
    id: str
    vector: list[float]
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float

    @property
    def score(self) -> float:
        """余弦距离转相似度分数，全项目统一使用该公式。"""
        return round(max(0.0, 1.0 - self.distance), 4)


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Chroma 只接受标量元数据值。"""
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned


def get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    settings.resolved_chroma_dir.mkdir(parents=True, exist_ok=True)
    if not str(settings.resolved_chroma_dir).isascii():
        logger.warning(
            "向量目录包含非 ASCII 字符（%s）：Chroma 在此类路径下写入较大索引可能丢失数据，"
            "建议通过 CHROMA_DIR 指定纯英文目录",
            settings.resolved_chroma_dir,
        )
    _client = chromadb.PersistentClient(
        path=str(settings.resolved_chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )
    _collection = _client.get_or_create_collection(
        name=settings.chroma_collection,
        metadata={"hnsw:space": "cosine", "embedding_model": settings.embedding_model},
    )
    logger.info("向量集合就绪：%s", settings.chroma_collection)
    return _collection


def upsert(records: list[VectorRecord], batch_size: int = 64) -> int:
    if not records:
        return 0
    collection = get_collection()
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        collection.upsert(
            ids=[record.id for record in batch],
            embeddings=[record.vector for record in batch],
            documents=[record.document for record in batch],
            metadatas=[_clean_metadata(record.metadata) for record in batch],
        )
    return len(records)


def delete_by_document(document_id: str) -> int:
    collection = get_collection()
    existing = collection.get(where={"document_id": document_id}, include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def query(
    vector: list[float],
    top_k: int = 8,
    document_ids: list[str] | None = None,
) -> list[VectorHit]:
    collection = get_collection()
    where = {"document_id": {"$in": document_ids}} if document_ids else None
    result = collection.query(
        query_embeddings=[vector],
        n_results=max(1, top_k),
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    ids = (result.get("ids") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return [
        VectorHit(id=item_id, document=text or "", metadata=meta or {}, distance=float(distance))
        for item_id, text, meta, distance in zip(ids, documents, metadatas, distances, strict=False)
    ]


def count(document_id: str | None = None) -> int:
    collection = get_collection()
    if document_id:
        existing = collection.get(where={"document_id": document_id}, include=[])
        return len(existing.get("ids") or [])
    return collection.count()


def reset_collection() -> None:
    """测试或重建索引时清空集合。"""
    global _collection
    collection = get_collection()
    ids = collection.get(include=[]).get("ids") or []
    if ids:
        collection.delete(ids=ids)
    _collection = None


def drop_collection() -> None:
    """删除整个集合并重建。

    索引文件损坏（例如进程被强杀）时使用：元数据还在但 HNSW 数据结构已丢失，
    只能整表重建，随后按文档重新入库。
    """
    global _client, _collection
    if _client is None:
        get_collection()
    try:
        _client.delete_collection(settings.chroma_collection)  # type: ignore[union-attr]
    except Exception as exc:  # 集合不存在时忽略
        logger.warning("删除集合失败（可能不存在）：%s", exc)
    _collection = None
    get_collection()
