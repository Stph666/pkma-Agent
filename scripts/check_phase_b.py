"""阶段 B 验收脚本：真实上传三类样例，核对状态、切片数与向量一致性。

依赖：MySQL 已建库（pkma）、向量模型已缓存。
用法：python scripts/check_phase_b.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import check_database, session_scope  # noqa: E402
from app.integrations import vector_store  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
EXPECTED = ["检索增强生成技术说明.pdf", "知识图谱与实体关系.docx", "个人知识管理笔记.txt"]


def wait_ready(client: TestClient, document_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/documents/{document_id}")
        payload = response.json()
        if payload.get("status") in {"ready", "failed"}:
            return payload
        time.sleep(1.0)
    return payload


def upload(client: TestClient, path: Path) -> tuple[int, dict]:
    files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
    response = client.post("/api/documents", files=files)
    return response.status_code, response.json()


def main() -> int:
    ok, detail = check_database()
    print(f"[数据库] {detail}")
    if not ok:
        return 1

    failures: list[str] = []
    with TestClient(app) as client:
        print("\n=== 1. 健康检查 ===")
        health = client.get("/api/health").json()
        print(f"status={health['status']} db={health['database']['ok']} llm={health['llm']['ok']}")

        print("\n=== 2. 三类样例上传与就绪 ===")
        results = []
        for name in EXPECTED:
            path = SAMPLES / name
            if not path.exists():
                failures.append(f"样例缺失：{name}")
                continue
            code, body = upload(client, path)
            if code == 409:
                existing_id = body.get("detail", {}).get("document_id")
                print(f"{name}: 已存在（409），复用 {existing_id[:8]}")
                document_id = existing_id
            elif code == 201:
                document_id = body["id"]
            else:
                failures.append(f"{name} 上传失败：{code} {body}")
                continue

            info = wait_ready(client, document_id)
            status = info.get("status")
            chunks = info.get("chunk_count", 0)
            vectors = vector_store.count(document_id)
            pages = sorted(
                {
                    f"{chunk['page_start']}-{chunk['page_end']}"
                    for chunk in info.get("chunks", [])
                    if chunk.get("page_start")
                }
            )
            results.append((name, status, chunks, vectors, pages, document_id))
            print(
                f"{name}: 状态={status} 切片={chunks} 向量={vectors} 页码={pages or '—'}"
                + (f" 错误={info.get('error_message')}" if status != "ready" else "")
            )
            if status != "ready":
                failures.append(f"{name} 未就绪：{info.get('error_message')}")
            if chunks != vectors:
                failures.append(f"{name} 切片数与向量数不一致：{chunks} vs {vectors}")
            if chunks == 0:
                failures.append(f"{name} 没有切片")

        print("\n=== 3. 列表与筛选 ===")
        listing = client.get("/api/documents").json()
        print(f"总数={listing['total']} 状态统计={listing['counts']}")
        ready_only = client.get("/api/documents", params={"status": "ready"}).json()
        print(f"ready 筛选：{ready_only['total']} 条")
        if any(item["status"] != "ready" for item in ready_only["items"]):
            failures.append("状态筛选返回了非 ready 文档")

        print("\n=== 4. 异常输入 ===")
        cases = [
            ("旧版 DOC", "说明.doc", b"fake doc content", {415, 400}),
            ("空文件", "空.txt", b"", {400, 415}),
            ("Markdown", "笔记.md", b"# title", {415}),
        ]
        for label, filename, content, expected_codes in cases:
            response = client.post("/api/documents", files={"file": (filename, content, "application/octet-stream")})
            body = response.json()
            mark = "通过" if response.status_code in expected_codes else "失败"
            print(f"{label}: {response.status_code} {body.get('code')} -> {mark}")
            if response.status_code not in expected_codes:
                failures.append(f"{label} 未按预期拒绝")

        print("\n=== 5. 重复上传 ===")
        code, body = upload(client, SAMPLES / "个人知识管理笔记.txt")
        print(f"重复上传：{code} {body.get('code')}")
        if code != 409:
            failures.append("重复上传未返回 409")

        print("\n=== 6. 删除清理 ===")
        temp = io.BytesIO()
        from docx import Document as Docx

        document = Docx()
        document.add_paragraph("这是一份用于验证删除清理的临时文档。")
        document.save(temp)
        response = client.post(
            "/api/documents",
            files={"file": ("临时删除验证.docx", temp.getvalue(), "application/octet-stream")},
        )
        temp_id = response.json()["id"]
        wait_ready(client, temp_id)
        with session_scope() as session:
            before_chunks = session.scalar(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == temp_id)
            )
        before_vectors = vector_store.count(temp_id)
        client.delete(f"/api/documents/{temp_id}")
        after_chunks = 0
        with session_scope() as session:
            after_chunks = session.scalar(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == temp_id)
            )
            still_there = session.get(Document, temp_id)
        after_vectors = vector_store.count(temp_id)
        print(
            f"删除前 切片={before_chunks} 向量={before_vectors}；"
            f"删除后 切片={after_chunks} 向量={after_vectors} 记录={still_there}"
        )
        if after_chunks or after_vectors or still_there is not None:
            failures.append("删除后仍有残留数据")

    print("\n=== 结果 ===")
    if failures:
        for item in failures:
            print(f"[失败] {item}")
        return 1
    print(f"[通过] 三类样例全部 ready，共 {sum(r[2] for r in results)} 个切片，向量与切片一一对应")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
