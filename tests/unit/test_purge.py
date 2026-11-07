"""重索引/失败清理必须连图谱一起清：这组用例防的是「文档删了，图谱还指向幽灵切片」。"""

from __future__ import annotations

import uuid

import pytest

from app.db import check_database, session_scope
from app.models import Document, DocumentChunk, GraphEdge, GraphNode
from app.services import ingestion

DB_OK, DB_DETAIL = check_database()
pytestmark = pytest.mark.skipif(not DB_OK, reason=f"MySQL 不可用：{DB_DETAIL}")


@pytest.fixture()
def seeded(monkeypatch):
    """造一份带切片、图谱节点和边的文档，并把向量删除替换成计数。"""
    monkeypatch.setattr(ingestion.vector_store, "delete_by_document", lambda document_id: None)

    document_id = str(uuid.uuid4())
    node_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    chunk_id = str(uuid.uuid4())

    with session_scope() as session:
        session.add(
            Document(
                id=document_id,
                original_name="test-purge.docx",
                stored_name="test-purge.docx",
                stored_path="data/uploads/test-purge.docx",
                extension=".docx",
                size=1,
                content_hash=str(uuid.uuid4()),
                status="ready",
                tags_json=[],
            )
        )
        session.flush()  # 文档先落库：切片与节点都指向它
        session.add(
            DocumentChunk(
                id=chunk_id,
                document_id=document_id,
                chunk_index=0,
                content="内容",
                content_hash=str(uuid.uuid4()),
                is_parent=True,
            )
        )
        for index, node_id in enumerate(node_ids):
            session.add(
                GraphNode(
                    id=node_id,
                    document_id=document_id,
                    name=f"实体{index}",
                    normalized_name=f"实体{index}",
                    entity_type="概念",
                    source_chunk_id=chunk_id,
                )
            )
        session.flush()  # 先落节点，否则外键约束会拦边
        session.add(
            GraphEdge(
                id=str(uuid.uuid4()),
                document_id=document_id,
                source_node_id=node_ids[0],
                target_node_id=node_ids[1],
                relation="相关",
                source_chunk_id=chunk_id,
            )
        )

    yield document_id

    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is not None:
            session.delete(document)


def test_purge_removes_chunks_and_graph(seeded):
    ingestion.purge_document_artifacts(seeded)

    with session_scope() as session:
        assert session.query(DocumentChunk).filter_by(document_id=seeded).count() == 0
        assert session.query(GraphNode).filter_by(document_id=seeded).count() == 0
        assert session.query(GraphEdge).filter_by(document_id=seeded).count() == 0


def test_fail_document_also_clears_graph(seeded):
    ingestion._fail_document(seeded, "模拟失败")

    with session_scope() as session:
        document = session.get(Document, seeded)
        assert document is not None and document.status == "failed"
        assert session.query(GraphNode).filter_by(document_id=seeded).count() == 0
        assert session.query(GraphEdge).filter_by(document_id=seeded).count() == 0
