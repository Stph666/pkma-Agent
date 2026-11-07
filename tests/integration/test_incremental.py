"""阶段 E 集成测试：同名检测、覆盖重传、批量重试、重启恢复、删除清理。"""

from __future__ import annotations

import io
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import check_database, session_scope

DB_OK, DB_DETAIL = check_database()
pytestmark = pytest.mark.skipif(not DB_OK, reason=f"MySQL 不可用：{DB_DETAIL}")


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


PREFIXES = ("同名检测用例", "哈希A", "哈希B", "重试用例", "重启恢复用例", "删除清理用例")


def _purge_test_documents() -> None:
    from app.models import Document
    from app.services import ingestion

    with session_scope() as session:
        stale = [
            document.id
            for document in session.scalars(select(Document)).all()
            if document.original_name.startswith(PREFIXES)
        ]
    for document_id in stale:
        try:
            ingestion.delete_document(document_id)
        except Exception:
            pass


@pytest.fixture(scope="module", autouse=True)
def _cleanup(client):
    # 先清上一次运行可能残留的测试文档，避免同名/同哈希导致误判
    _purge_test_documents()
    yield
    _purge_test_documents()


@pytest.fixture(autouse=True)
def _no_enrich(monkeypatch):
    """测试只关心入库，不触发摘要/图谱的模型调用。"""
    from app.config import settings

    monkeypatch.setattr(settings, "enrich_on_ingest", False)


def _wait(client: TestClient, document_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/documents/{document_id}")
        if response.status_code == 404:
            return {"status": "deleted"}
        payload = response.json()
        if payload.get("status") in {"ready", "failed"}:
            return payload
        time.sleep(1.0)
    return payload


def _upload(client: TestClient, name: str, text: str, overwrite: bool = False):
    return _upload_bytes(client, name, _docx_bytes(text), overwrite=overwrite)


def _upload_bytes(client: TestClient, name: str, payload: bytes, overwrite: bool = False):
    files = {"file": (name, payload, "application/octet-stream")}
    data = {"overwrite": "true"} if overwrite else None
    return client.post("/api/documents", files=files, data=data)


def test_duplicate_name_requires_overwrite(client: TestClient):
    name = "同名检测用例.docx"
    first = _upload(client, name, "第一版内容：向量检索与切片。")
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    _wait(client, first_id)

    second = _upload(client, name, "第二版内容：完全不同的正文，用于验证同名冲突。")
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_name"
    assert second.json()["detail"]["document_id"] == first_id

    # 覆盖重传：旧记录应被替换
    third = _upload(client, name, "第二版内容：完全不同的正文，用于验证同名冲突。", overwrite=True)
    assert third.status_code == 201, third.text
    new_id = third.json()["id"]
    assert new_id != first_id
    assert client.get(f"/api/documents/{first_id}").status_code == 404
    _wait(client, new_id)
    client.delete(f"/api/documents/{new_id}")


def test_same_content_still_blocked_by_hash(client: TestClient):
    content = _docx_bytes(f"哈希去重用例：同样的内容换个文件名也不该重复入库。{uuid.uuid4().hex}")
    first = _upload_bytes(client, "哈希A.docx", content)
    assert first.status_code == 201
    document_id = first.json()["id"]
    _wait(client, document_id)

    second = _upload_bytes(client, "哈希B.docx", content)
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_document"
    client.delete(f"/api/documents/{document_id}")


def test_retry_failed_reprocesses_documents(client: TestClient):
    response = _upload(client, "重试用例.docx", "这份文档会被手动标记为失败，然后批量重试。")
    document_id = response.json()["id"]
    _wait(client, document_id)

    from app.models import Document

    with session_scope() as session:
        document = session.get(Document, document_id)
        document.status = "failed"
        document.error_message = "手动构造的失败"

    retry = client.post("/api/documents/retry-failed")
    assert retry.status_code == 200
    assert retry.json()["retried"] >= 1

    payload = _wait(client, document_id)
    assert payload["status"] == "ready"
    client.delete(f"/api/documents/{document_id}")


def test_restart_recovery_marks_processing_as_failed(client: TestClient):
    response = _upload(client, "重启恢复用例.docx", "模拟应用在上传处理过程中被强制结束。")
    document_id = response.json()["id"]
    _wait(client, document_id)

    from app.models import Document
    from app.services.ingestion import recover_interrupted

    with session_scope() as session:
        document = session.get(Document, document_id)
        document.status = "processing"
        document.error_message = None

    with session_scope() as session:
        recovered = recover_interrupted(session)
    assert recovered >= 1

    detail = client.get(f"/api/documents/{document_id}").json()
    assert detail["status"] == "failed"
    assert "重启" in (detail["error_message"] or "")
    client.delete(f"/api/documents/{document_id}")


def test_delete_removes_graph_and_chunks(client: TestClient):
    response = _upload(client, "删除清理用例.docx", "删除后切片、向量、图谱节点都不应残留。")
    document_id = response.json()["id"]
    _wait(client, document_id)

    from app.models import DocumentChunk, GraphEdge, GraphNode

    with session_scope() as session:
        node = GraphNode(
            document_id=document_id,
            normalized_name="测试实体",
            name="测试实体",
            entity_type="概念",
        )
        session.add(node)
        session.flush()
        session.add(
            GraphEdge(
                document_id=document_id,
                source_node_id=node.id,
                target_node_id=node.id,
                relation="相关",
            )
        )

    client.delete(f"/api/documents/{document_id}")

    with session_scope() as session:
        chunks = session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        nodes = session.scalar(
            select(func.count()).select_from(GraphNode).where(GraphNode.document_id == document_id)
        )
        edges = session.scalar(
            select(func.count()).select_from(GraphEdge).where(GraphEdge.document_id == document_id)
        )
    assert (chunks, nodes, edges) == (0, 0, 0)
