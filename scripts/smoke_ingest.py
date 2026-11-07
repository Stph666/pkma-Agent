"""入库链路冒烟脚本（不依赖本机 MySQL）。

用临时 SQLite 库与临时 Chroma 目录跑一遍：解析 → 切片 → 向量化 → 落库 → 检索，
用于在数据库尚未就绪时验证管线本身是否可用。

用法：
    python scripts/smoke_ingest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP_DIR = Path(tempfile.mkdtemp(prefix="pkma-smoke-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TEMP_DIR / 'smoke.db').as_posix()}"
os.environ["DATA_DIR"] = str(TEMP_DIR / "data")
os.environ["CHROMA_DIR"] = str(TEMP_DIR / "chroma")
os.environ["UPLOAD_DIR"] = str(TEMP_DIR / "uploads")
os.environ["AUTO_CREATE_TABLES"] = "true"
# 复用项目内的模型缓存，避免临时目录触发重复下载
os.environ["HF_HOME"] = str(ROOT / "data" / "models")

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.integrations import vector_store  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services import ingestion  # noqa: E402


def build_docx(path: Path) -> None:
    from docx import Document as Docx

    document = Docx()
    document.add_heading("检索增强生成", level=1)
    document.add_paragraph(
        "检索增强生成（RAG）将向量检索与语言模型生成结合：先从知识库召回相关片段，再让模型基于片段作答。"
    )
    document.add_paragraph(
        "切片策略直接影响召回质量。过长的切片会引入噪声，过短则会丢失上下文。本项目采用约 500 字、80 字重叠的递归切片。"
    )
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "取值"
    table.cell(1, 0).text = "chunk_size"
    table.cell(1, 1).text = "500"
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


def main() -> int:
    print(f"临时目录：{TEMP_DIR}")
    settings.ensure_directories()
    init_db()

    source = TEMP_DIR / "sample.docx"
    build_docx(source)

    started = time.time()
    document = ingestion.register_upload(source, "示例-检索增强.docx")
    ingestion.ingest_document(document.id)
    elapsed = time.time() - started

    with session_scope() as session:
        stored = session.get(Document, document.id)
        chunk_rows = session.scalars(
            DocumentChunk.__table__.select().where(DocumentChunk.document_id == document.id)
        ).all()
        chunk_total = len(chunk_rows)
        status = stored.status
        error = stored.error_message

    print(f"状态：{status}　切片：{chunk_total}　耗时：{elapsed:.1f}s")
    if error:
        print(f"错误：{error}")
        return 1
    if status != "ready" or chunk_total == 0:
        print("[失败] 未达到 ready 或没有切片")
        return 1

    vector_total = vector_store.count(document.id)
    print(f"Chroma 向量数：{vector_total}")
    if vector_total != chunk_total:
        print("[失败] 向量数与切片数不一致")
        return 1

    from app.integrations import embeddings

    hits = vector_store.query(embeddings.encode_query("切片长度怎么设置"), top_k=3)
    print("检索前 3 条：")
    for hit in hits:
        print(f"  - score={hit.score}  chunk={hit.metadata.get('chunk_index')}  {hit.document[:40]}")

    ingested = session_scope()
    with ingested as session:
        ingestion.delete_document(document.id)
        remaining = session.query(Document).count()

    print(f"删除后文档数：{remaining}　向量数：{vector_store.count()}")
    if remaining != 0 or vector_store.count() != 0:
        print("[失败] 删除未清理干净")
        return 1

    print("[通过] 入库、检索、删除链路正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
