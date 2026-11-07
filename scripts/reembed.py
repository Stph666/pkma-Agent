"""就地重建向量：换 embedding 模型后，用库里已有的子片重新编码。

不重新切片——`graph_nodes.source_chunk_id` 指向这些切片 id，
一旦 re-chunk 就会让图谱里的实体丢掉原文片段。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.integrations import embeddings, vector_store
from app.models import Document, DocumentChunk


def main() -> int:
    print(f"目标模型 {settings.embedding_model}", flush=True)
    with session_scope() as session:
        documents = {doc.id: doc for doc in session.scalars(select(Document)).all()}
        children = list(
            session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.is_parent.is_(False))
                .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            ).all()
        )
        rows = [
            (
                child.id,
                child.content,
                child.chunk_index,
                child.page_start,
                child.page_end,
                child.document_id,
            )
            for child in children
        ]

    print(f"待重建 {len(rows)} 片子片", flush=True)
    # 维度变了：必须整collection 删掉重建，delete-all 会保留旧的 512 维约束
    vector_store.drop_collection()
    vector_store.get_collection()

    started = time.time()
    batch = 256
    for start in range(0, len(rows), batch):
        chunk = rows[start : start + batch]
        vectors = embeddings.encode_texts([item[1] for item in chunk])
        records = [
            vector_store.VectorRecord(
                id=item[0],
                vector=vector,
                document=item[1],
                metadata={
                    "document_id": item[5],
                    "chunk_index": item[2],
                    "filename": documents[item[5]].original_name if item[5] in documents else "",
                    "page_start": item[3],
                    "page_end": item[4],
                    "category": (
                        (documents[item[5]].category if item[5] in documents else "") or ""
                    ),
                },
            )
            for item, vector in zip(chunk, vectors)
        ]
        vector_store.upsert(records, batch_size=min(2000, max(256, len(records))))
        done = min(start + batch, len(rows))
        print(f"{done}/{len(rows)} · {time.time() - started:.0f}s", flush=True)

    print(f"完成，集合内 {vector_store.count()} 条", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
