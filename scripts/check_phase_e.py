"""阶段 E 自检：孤儿图谱、切片与向量一致性、重复 chunk_index、同名文档。

用法：python scripts/check_phase_e.py
退出码 0 表示四项全部通过。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db import check_database, session_scope  # noqa: E402
from app.integrations import vector_store  # noqa: E402
from app.models import Document, DocumentChunk, GraphEdge, GraphNode  # noqa: E402


def check_orphan_graph() -> tuple[bool, str]:
    """图谱节点/边必须挂在存在的文档与切片上。"""
    with session_scope() as session:
        document_ids = {row for row in session.scalars(select(Document.id)).all()}
        chunk_ids = {row for row in session.scalars(select(DocumentChunk.id)).all()}
        nodes = list(session.scalars(select(GraphNode)).all())
        edges = list(session.scalars(select(GraphEdge)).all())

    bad_nodes = [
        node
        for node in nodes
        if node.document_id not in document_ids
        or (node.source_chunk_id and node.source_chunk_id not in chunk_ids)
    ]
    node_ids = {node.id for node in nodes}
    bad_edges = [
        edge
        for edge in edges
        if edge.document_id not in document_ids
        or edge.source_node_id not in node_ids
        or edge.target_node_id not in node_ids
        or (edge.source_chunk_id and edge.source_chunk_id not in chunk_ids)
    ]

    if bad_nodes or bad_edges:
        return False, f"孤儿节点 {len(bad_nodes)} 个、孤儿边 {len(bad_edges)} 条"
    return True, f"节点 {len(nodes)} 个、边 {len(edges)} 条，均无孤儿"


def check_vectors() -> tuple[bool, str]:
    """每份文档的子片数应等于其向量数。

    向量只建在子片上（父块给模型看，不进索引），所以这里必须排除 is_parent，
    否则每份文档都会「父块数」多于「向量数」而被误判。
    """
    with session_scope() as session:
        rows = session.execute(
            select(DocumentChunk.document_id, func.count())
            .where(DocumentChunk.is_parent.is_(False))
            .group_by(DocumentChunk.document_id)
        ).all()
        documents = {
            doc.id: (doc.original_name, doc.status)
            for doc in session.scalars(select(Document)).all()
        }

    mismatched = []
    ready_checked = 0
    for document_id, chunk_count in rows:
        name, status = documents.get(document_id, (document_id[:8], "?"))
        vector_count = vector_store.count(document_id)
        if int(chunk_count) != int(vector_count):
            mismatched.append(f"{name}（{status}）: {chunk_count} 子片 / {vector_count} 向量")
        elif status == "ready":
            ready_checked += 1

    if mismatched:
        return False, "；".join(mismatched)
    return True, f"{ready_checked} 份 ready 文档的子片数与向量数一致"


def check_duplicate_chunk_index() -> tuple[bool, str]:
    """同一文档内 chunk_index 不能重复。"""
    with session_scope() as session:
        rows = session.execute(
            select(DocumentChunk.document_id, DocumentChunk.chunk_index, func.count())
            .group_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            .having(func.count() > 1)
        ).all()
    if rows:
        return False, f"发现 {len(rows)} 处重复 chunk_index"
    return True, "无重复 chunk_index"


def check_duplicate_names() -> tuple[bool, str]:
    """同名文档只应保留一份（覆盖重传应替换旧记录）。"""
    with session_scope() as session:
        names = [document.original_name for document in session.scalars(select(Document)).all()]
    duplicated = {name: count for name, count in Counter(names).items() if count > 1}
    if duplicated:
        detail = "、".join(f"{name}×{count}" for name, count in duplicated.items())
        return False, f"同名文档 {len(duplicated)} 组：{detail}"
    return True, f"{len(names)} 份文档均无重名"


def main() -> int:
    ok, detail = check_database()
    print(f"[数据库] {detail}")
    if not ok:
        return 1

    checks = [
        ("孤儿图谱", check_orphan_graph),
        ("切片与向量一致", check_vectors),
        ("重复 chunk_index", check_duplicate_chunk_index),
        ("同名文档", check_duplicate_names),
    ]

    failures = 0
    print()
    for label, func_ in checks:
        try:
            passed, message = func_()
        except Exception as exc:  # pragma: no cover - 自检脚本需要给出可读错误
            passed, message = False, f"{type(exc).__name__}: {exc}"
        mark = "通过" if passed else "失败"
        print(f"[{mark}] {label}：{message}")
        failures += 0 if passed else 1

    print()
    if failures:
        print(f"[结果] {failures} 项未通过")
        return 1
    print("[结果] 四项自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
