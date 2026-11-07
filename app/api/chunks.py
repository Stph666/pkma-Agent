"""切片上下文：把一段命中片段扩展成「整个小节」，供阅读面板使用。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Document, DocumentChunk
from app.schemas import AppError

router = APIRouter(prefix="/api/chunks", tags=["chunks"])

HEADING = re.compile(r"^\s*(#{1,3}\s|第[0-9一二三四五六七八九十]+[章节]|[0-9]+(\.[0-9]+)+\s)")


def _resolve_parent(session: Session, chunk: DocumentChunk) -> DocumentChunk:
    if chunk.is_parent or not chunk.parent_id:
        return chunk
    parent = session.get(DocumentChunk, chunk.parent_id)
    return parent or chunk


@router.get("/{chunk_id}/context", summary="按切片取所在小节的完整内容")
def chunk_context(
    chunk_id: str,
    budget: int = Query(default=2600, ge=600, le=8000),
    session: Session = Depends(get_db),
) -> dict:
    chunk = session.get(DocumentChunk, chunk_id)
    if chunk is None:
        raise AppError("chunk_not_found", "片段不存在", status_code=404)

    anchor = _resolve_parent(session, chunk)
    siblings = list(
        session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == anchor.document_id, DocumentChunk.is_parent.is_(True))
            .order_by(DocumentChunk.chunk_index)
        ).all()
    )
    if not siblings:
        siblings = [anchor]

    index = next((position for position, item in enumerate(siblings) if item.id == anchor.id), 0)

    # 向前后扩展，遇到下一个标题行就停：这样看到的是「一节」而不是一刀切的字数
    start = index
    end = index
    size = len(anchor.content or "")
    while start > 0 and size < budget:
        candidate = siblings[start - 1]
        if HEADING.match((candidate.content or "").strip()[:40]) and size > budget * 0.5:
            break
        start -= 1
        size += len(candidate.content or "")
    while end < len(siblings) - 1 and size < budget:
        nxt = siblings[end + 1]
        if HEADING.match((nxt.content or "").strip()[:40]):
            break
        end += 1
        size += len(nxt.content or "")

    window = siblings[start : end + 1]
    text = "\n\n".join((item.content or "").strip() for item in window)

    document = session.get(Document, anchor.document_id)
    highlight = chunk.content or ""
    if highlight and highlight not in text:
        # 子片可能被父块拼接时改写过空白，退化为前 80 字定位
        probe = re.sub(r"\s+", "", highlight)[:80]
        compact = re.sub(r"\s+", "", text)
        highlight = highlight if probe and probe in compact else ""

    return {
        "chunk_id": chunk.id,
        "document_id": anchor.document_id,
        "filename": document.original_name if document else "未知文档",
        "page_start": window[0].page_start,
        "page_end": window[-1].page_end,
        "location": (
            f"第 {window[0].page_start} 页"
            if window[0].page_start == window[-1].page_end
            else f"第 {window[0].page_start}–{window[-1].page_end} 页"
        ),
        "text": text,
        "highlight": highlight,
        "chunks": len(window),
    }
