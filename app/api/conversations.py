"""会话接口：列表、新建、消息、重命名、删除。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Message

from app.db import get_db
from app.schemas import (
    ConversationCreate,
    ConversationListResponse,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)
from app.services import conversations as service

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _to_out(conversation, message_count: int = 0) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        mode=conversation.mode,
        message_count=message_count,
        document_ids=list(conversation.document_scope_json or []),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.get("", response_model=ConversationListResponse, summary="会话列表")
def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> ConversationListResponse:
    items = service.list_conversations(session, limit=limit)
    counts = {
        conversation_id: total
        for conversation_id, total in session.execute(
            select(Message.conversation_id, func.count()).group_by(Message.conversation_id)
        ).all()
    }
    return ConversationListResponse(
        items=[_to_out(item, int(counts.get(item.id, 0))) for item in items],
        total=len(items),
    )


@router.post("", response_model=ConversationOut, status_code=201, summary="新建会话")
def create_conversation(payload: ConversationCreate, session: Session = Depends(get_db)) -> ConversationOut:
    conversation = service.create_conversation(
        session,
        title=payload.title,
        mode=payload.mode,
        document_ids=payload.document_ids,
    )
    session.commit()
    return _to_out(conversation)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut], summary="历史消息")
def get_messages(conversation_id: str, session: Session = Depends(get_db)) -> list[MessageOut]:
    messages = service.list_messages(session, conversation_id)
    return [
        MessageOut(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,
            content=message.content,
            mode=message.mode,
            status=message.status,
            citations=list(message.citations_json or []),
            error_message=message.error_message,
            created_at=message.created_at,
        )
        for message in messages
    ]


@router.patch("/{conversation_id}", response_model=ConversationOut, summary="重命名或修改文档范围")
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    session: Session = Depends(get_db),
) -> ConversationOut:
    conversation = service.get_conversation(session, conversation_id)
    if payload.title is not None:
        conversation.title = payload.title.strip()[:80] or conversation.title
    if payload.document_ids is not None:
        conversation.document_scope_json = payload.document_ids
    session.commit()
    return _to_out(conversation)


@router.delete("/{conversation_id}", status_code=204, summary="删除会话及消息")
def remove_conversation(conversation_id: str, session: Session = Depends(get_db)) -> None:
    service.delete_conversation(session, conversation_id)
    session.commit()
