"""会话服务：会话、消息与提示词历史。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Message
from app.schemas import AppError

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "新对话"
TITLE_LENGTH = 20


def derive_title(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return DEFAULT_TITLE
    return cleaned[:TITLE_LENGTH]


def create_conversation(
    session: Session,
    *,
    title: str | None = None,
    mode: str = "knowledge",
    document_ids: list[str] | None = None,
) -> Conversation:
    conversation = Conversation(
        title=(title or DEFAULT_TITLE)[:80],
        mode=mode,
        document_scope_json=document_ids or [],
    )
    session.add(conversation)
    session.flush()
    return conversation


def list_conversations(session: Session, limit: int = 50) -> list[Conversation]:
    return list(session.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)).all())


def get_conversation(session: Session, conversation_id: str) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise AppError("conversation_not_found", "会话不存在", status_code=404)
    return conversation


def delete_conversation(session: Session, conversation_id: str) -> None:
    conversation = get_conversation(session, conversation_id)
    session.delete(conversation)


def append_message(
    session: Session,
    conversation: Conversation,
    *,
    role: str,
    content: str,
    mode: str,
    citations: list[dict] | None = None,
    status: str = "done",
    error_message: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        role=role,
        content=content,
        mode=mode,
        status=status,
        citations_json=citations or [],
        error_message=error_message,
    )
    session.add(message)
    session.flush()

    # 首条用户消息决定会话标题
    if role == "user" and conversation.title in {DEFAULT_TITLE, ""}:
        conversation.title = derive_title(content)
    session.flush()
    return message


def list_messages(session: Session, conversation_id: str, limit: int = 200) -> list[Message]:
    get_conversation(session, conversation_id)
    return list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
            .limit(limit)
        ).all()
    )


def delete_last_assistant(session: Session, conversation_id: str) -> Message | None:
    """删除最后一条助手消息，用于「重新生成」时替换而不是追加。"""
    message = session.scalar(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if message is not None:
        session.delete(message)
        session.flush()
    return message


SUMMARY_KEEP_RECENT = 6
SUMMARY_TRIGGER = 10
_SUMMARY_CACHE: dict[str, tuple[int, str]] = {}


def _summarize_messages(conversation_id: str, messages: list[Message]) -> str:
    """把较早的消息压成一段摘要，按「会话 + 消息数」缓存，避免每轮都调用。"""
    key = f"{conversation_id}:{len(messages)}"
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None:
        return cached[1]

    transcript = "\n".join(f"{'用户' if m.role == 'user' else '助手'}：{m.content[:300]}" for m in messages)
    summary = ""
    try:
        from app.integrations import llm

        summary = llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "把下面的对话压缩成 150 字以内的中文摘要，保留：讨论的主题、得出的结论、"
                        "用户提出的约束或偏好。只输出摘要本身。"
                    ),
                },
                {"role": "user", "content": transcript[:6000]},
            ],
            temperature=0.2,
            max_tokens=400,
        ).strip()
    except Exception:
        summary = ""

    if not summary:
        summary = transcript[:600].replace("\n", " ")
    _SUMMARY_CACHE[key] = (len(messages), summary)
    return summary


def history_for_prompt(session: Session, conversation_id: str, limit: int = 10) -> list[dict]:
    """取最近若干条已完成消息，供提示词使用（不含本轮问题）。

    超过 SUMMARY_TRIGGER 条时，把较早的部分压成摘要放在最前面，
    只保留最近 SUMMARY_KEEP_RECENT 条原文——避免长对话把早期关键信息整段丢掉。
    """
    messages = list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.status == "done")
            .order_by(Message.created_at)
            .limit(200)
        ).all()
    )
    if not messages:
        return []

    if len(messages) > SUMMARY_TRIGGER:
        older = messages[:-SUMMARY_KEEP_RECENT]
        recent = messages[-SUMMARY_KEEP_RECENT:]
        summary = _summarize_messages(conversation_id, older)
        history: list[dict] = []
        if summary:
            history.append(
                {
                    "role": "system",
                    "content": f"【更早对话的摘要】{summary}\n（摘要仅用于理解指代，事实仍以本轮片段为准）",
                }
            )
        history.extend({"role": m.role, "content": m.content} for m in recent)
        return history

    return [{"role": m.role, "content": m.content} for m in messages[-limit:]]
