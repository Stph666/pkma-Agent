"""全局搜索与动态建议：搜索命中对话标题与消息正文；建议来自知识库现有实体。"""

from __future__ import annotations

import re
from collections import Counter

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Conversation, DocumentChunk, GraphEdge, GraphNode, Message
from app.services import conversations as conversation_service

router = APIRouter(prefix="/api", tags=["search"])


TEMPLATES = (
    "「{name}」是什么？",
    "关于「{name}」，文档里怎么解释？",
    "「{name}」和其他概念是什么关系？",
    "文档里哪里讲到了「{name}」？",
)


def _snippet(text: str, keyword: str, width: int = 46) -> str:
    """截取关键词附近的片段，前端据此高亮。"""
    clean = re.sub(r"\s+", " ", text or "").strip()
    index = clean.lower().find(keyword.lower())
    if index < 0:
        return clean[: width * 2]
    start = max(0, index - width // 2)
    end = min(len(clean), index + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


@router.get("/search", summary="搜索对话与消息")
def search(
    q: str = Query(min_length=1, max_length=60),
    limit: int = Query(default=12, ge=1, le=50),
    session: Session = Depends(get_db),
) -> dict:
    keyword = q.strip()
    like = f"%{keyword}%"

    talks = list(
        session.scalars(
            select(Conversation)
            .where(Conversation.title.like(like))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        ).all()
    )

    hits = list(
        session.scalars(
            select(Message)
            .where(or_(Message.content.like(like), Message.error_message.like(like)))
            .order_by(Message.created_at.desc())
            .limit(limit * 2)
        ).all()
    )
    titles = {
        item.id: item.title
        for item in session.scalars(
            select(Conversation).where(Conversation.id.in_({hit.conversation_id for hit in hits}))
        ).all()
    } if hits else {}

    return {
        "query": keyword,
        "conversations": [
            {"id": item.id, "title": item.title, "updated_at": item.updated_at.isoformat()}
            for item in talks
        ],
        "messages": [
            {
                "id": hit.id,
                "conversation_id": hit.conversation_id,
                "title": titles.get(hit.conversation_id, "未命名会话"),
                "role": hit.role,
                "snippet": _snippet(hit.content, keyword),
                "created_at": hit.created_at.isoformat(),
            }
            for hit in hits[: limit]
        ],
    }


@router.get("/suggestions", summary="根据知识库生成首页建议问题")
def suggestions(
    n: int = Query(default=3, ge=1, le=6),
    session: Session = Depends(get_db),
) -> dict:
    """用图谱里连接度最高的实体造问题，低成本且随知识库变化。"""
    degree: Counter[str] = Counter()
    for edge in session.scalars(select(GraphEdge)).all():
        degree[edge.source_node_id] += 1
        degree[edge.target_node_id] += 1

    nodes = list(session.scalars(select(GraphNode)).all())
    ranked = sorted(nodes, key=lambda node: degree.get(node.id, 0), reverse=True)

    items: list[str] = []
    seen: set[str] = set()
    for node in ranked:
        name = (node.name or "").strip()
        if not name or len(name) < 2 or len(name) > 18 or name.lower() in seen:
            continue
        seen.add(name.lower())
        template = TEMPLATES[len(items) % len(TEMPLATES)]
        items.append(template.format(name=name))
        if len(items) >= n:
            break

    # 图谱还没建好时，退化成用文档里的标题行造问题
    if len(items) < n:
        rows = session.scalars(select(DocumentChunk.content).limit(60)).all()
        for content in rows:
            first = (content or "").strip().split("\n")[0].strip()
            if 6 <= len(first) <= 24 and first not in seen and not first.endswith("。"):
                seen.add(first)
                items.append(f"{first}讲的是什么？")
            if len(items) >= n:
                break

    return {"items": items[:n]}
