"""文档接口集成测试：上传 → 就绪 → 列表 → 详情 → 重新索引 → 删除。

依赖真实 MySQL（库 pkma 与账号 pkma）。数据库不可用时整组跳过。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from app.db import check_database

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


@pytest.fixture(autouse=True)
def cleanup_test_documents(request):
    """无论用例是否通过，都清掉本次创建的测试文档，避免脏数据残留。"""
    yield
    if request.node.name == "test_upload_ingest_list_detail_delete":
        return  # 该用例自身已校验删除
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Document
    from app.services import ingestion

    prefixes = ("集成测试", "重复", "临时删除验证")
    try:
        with session_scope() as session:
            stale = [
                document.id
                for document in session.scalars(select(Document)).all()
                if document.original_name.startswith(prefixes)
            ]
        for document_id in stale:
            ingestion.delete_document(document_id)
    except Exception:  # pragma: no cover - 清理失败不应让测试报错
        pass


def _wait_ready(client: TestClient, document_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        response = client.get(f"/api/documents/{document_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"ready", "failed"}:
            return payload
        time.sleep(1.0)
    pytest.fail(f"文档在 {timeout}s 内未完成处理：{payload.get('status')}")


def test_upload_ingest_list_detail_delete(client: TestClient):
    content = _docx_bytes("向量检索把问题映射到向量空间，再按相似度召回片段。检索增强生成据此约束模型回答。")
    files = {"file": ("test-集成检索.docx", content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}

    upload = client.post("/api/documents", files=files)
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    assert upload.json()["status"] == "processing"

    detail = _wait_ready(client, document_id)
    assert detail["status"] == "ready", detail.get("error_message")
    assert detail["chunk_count"] >= 1
    assert detail["chunks"]
    assert detail["chunks"][0]["content"]

    listing = client.get("/api/documents")
    assert listing.status_code == 200
    assert any(item["id"] == document_id for item in listing.json()["items"])

    status_filtered = client.get("/api/documents", params={"status": "ready"})
    assert all(item["status"] == "ready" for item in status_filtered.json()["items"])

    reindex = client.post(f"/api/documents/{document_id}/reindex")
    assert reindex.status_code == 200
    again = _wait_ready(client, document_id)
    assert again["status"] == "ready"
    assert again["chunk_count"] == detail["chunk_count"], "重建索引后切片数量不应增长"

    delete = client.delete(f"/api/documents/{document_id}")
    assert delete.status_code == 204
    assert client.get(f"/api/documents/{document_id}").status_code == 404


def test_duplicate_upload_rejected(client: TestClient):
    content = _docx_bytes("重复内容检测：相同哈希的文件不应重复入库。")
    files = {"file": ("重复.docx", content, "application/octet-stream")}

    first = client.post("/api/documents", files=files)
    assert first.status_code == 201
    document_id = first.json()["id"]
    _wait_ready(client, document_id)

    second = client.post(
        "/api/documents",
        files={"file": ("test-重复副本.docx", content, "application/octet-stream")},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_document"

    client.delete(f"/api/documents/{document_id}")


def test_upload_rejects_unsupported_extension(client: TestClient):
    response = client.post(
        "/api/documents",
        files={"file": ("test-说明.md", b"# hello", "text/markdown")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_format"


def test_upload_rejects_empty_file(client: TestClient):
    response = client.post("/api/documents", files={"file": ("test-空.txt", b"", "text/plain")})
    assert response.status_code in {400, 415}
    assert response.json()["code"] in {"empty_file", "unsupported_format"}

def test_large_document_warning_header_is_ascii_safe(client: TestClient, monkeypatch):
    """中文提示不能直接塞进响应头（latin-1 会 500），必须是百分号编码。"""
    from urllib.parse import unquote

    from app.config import settings

    monkeypatch.setattr(settings, "max_document_chars", 10)
    response = client.post(
        "/api/documents",
        files={"file": ("test-超限提示.docx", _docx_bytes("这是一段用于触发超限提示的测试文本。"), "application/octet-stream")},
        data={"force": "true", "overwrite": "true"},
    )
    assert response.status_code == 201, response.text

    raw = response.headers.get("X-Ingest-Warning", "")
    assert raw, "超过建议上限应返回提示"
    raw.encode("latin-1")  # 头值必须能按 latin-1 编码，否则真实环境会 500
    assert "强制" in unquote(raw)
