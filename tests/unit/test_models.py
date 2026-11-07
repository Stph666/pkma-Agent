"""ORM 模型测试：建表、级联删除与 JSON 字段。

使用 SQLite 内存库验证模型结构，不依赖本机 MySQL。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app import models
from app.db import Base


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://")

    # SQLite 默认不启用外键，需显式开启才能验证 ON DELETE CASCADE
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _document(**overrides) -> models.Document:
    payload = {
        "original_name": "示例.pdf",
        "stored_name": "abc.pdf",
        "stored_path": "data/uploads/abc.pdf",
        "extension": ".pdf",
        "size": 1024,
        "content_hash": "hash-1",
        "status": "ready",
        "summary": "摘要",
        "category": "课程资料",
        "tags_json": ["RAG", "向量检索"],
    }
    payload.update(overrides)
    return models.Document(**payload)


def test_all_tables_defined():
    expected = {
        "documents",
        "document_chunks",
        "conversations",
        "messages",
        "graph_nodes",
        "graph_edges",
    }
    assert expected.issubset(set(Base.metadata.tables))


def test_document_persist_and_timestamps(session: Session):
    doc = _document()
    session.add(doc)
    session.commit()

    stored = session.scalar(select(models.Document))
    assert stored.id and len(stored.id) == 36
    assert stored.tags_json == ["RAG", "向量检索"]
    assert isinstance(stored.created_at, datetime)
    assert stored.updated_at >= stored.created_at


def test_chunk_cascade_delete(session: Session):
    doc = _document()
    doc.chunks = [
        models.DocumentChunk(chunk_index=0, content="第一段", content_hash="c0", page_start=1, page_end=1),
        models.DocumentChunk(chunk_index=1, content="第二段", content_hash="c1", page_start=2, page_end=2),
    ]
    session.add(doc)
    session.commit()

    session.delete(doc)
    session.commit()

    assert session.scalar(select(func.count()).select_from(models.DocumentChunk)) == 0


def test_conversation_messages_cascade(session: Session):
    conversation = models.Conversation(title="新对话", mode="knowledge", document_scope_json=[])
    conversation.messages = [
        models.Message(role="user", content="什么是 RAG？", mode="knowledge"),
        models.Message(role="assistant", content="RAG 是……", mode="knowledge", citations_json=[]),
    ]
    session.add(conversation)
    session.commit()

    assert session.scalar(select(func.count()).select_from(models.Message)) == 2
    session.delete(conversation)
    session.commit()
    assert session.scalar(select(func.count()).select_from(models.Message)) == 0


def test_graph_edges_reference_nodes(session: Session):
    doc = _document(content_hash="hash-graph")
    head = models.GraphNode(document=doc, normalized_name="lightrag", name="LightRAG", entity_type="系统")
    tail = models.GraphNode(document=doc, normalized_name="知识图谱", name="知识图谱", entity_type="概念")
    session.add_all([doc, head, tail])
    session.flush()

    session.add(
        models.GraphEdge(document_id=doc.id, source_node_id=head.id, target_node_id=tail.id, relation="使用")
    )
    session.commit()

    assert session.scalar(select(func.count()).select_from(models.GraphEdge)) == 1
    session.delete(doc)
    session.commit()
    assert session.scalar(select(func.count()).select_from(models.GraphNode)) == 0
    assert session.scalar(select(func.count()).select_from(models.GraphEdge)) == 0
