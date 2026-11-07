"""问答编排：把会话、检索、生成串成事件流。

接口层（SSE）与界面层（Gradio 流式输出）共用这里的逻辑，避免两份实现走偏。
事件顺序：meta → delta* → citations → done；出错时 error → done。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from app.config import settings
from app.db import session_scope
from app.integrations import llm
from app.schemas import AppError
from app.services import conversations as conversation_service
from app.services import generation, retrieval

logger = logging.getLogger(__name__)

Event = tuple[str, dict]


def _open_conversation(
    conversation_id: str | None,
    mode: str,
    document_ids: list[str],
) -> tuple[str, list[dict], str, list[str]]:
    """取或建会话，返回 (会话ID, 历史, 模式, 文档范围)。历史不含本轮问题。"""
    with session_scope() as session:
        if conversation_id:
            conversation = conversation_service.get_conversation(session, conversation_id)
        else:
            conversation = conversation_service.create_conversation(
                session, mode=mode, document_ids=document_ids
            )
        conversation.mode = mode
        if document_ids:
            conversation.document_scope_json = document_ids

        history = conversation_service.history_for_prompt(session, conversation.id)
        return conversation.id, history, conversation.mode, list(conversation.document_scope_json or [])



def _prepare_regenerate(conversation_id: str, message: str) -> list[dict]:
    """重新生成：删掉上一条助手消息，复用已有的用户消息，不再追加一条。

    之前 regenerate 参数只是接收、没有任何分支处理，导致库里变成
    user → assistant → user → assistant，刷新历史会看到重复问题。
    """
    with session_scope() as session:
        conversation = conversation_service.get_conversation(session, conversation_id)
        conversation_service.delete_last_assistant(session, conversation_id)

        messages = conversation_service.list_messages(session, conversation_id)
        if messages and messages[-1].role == "user":
            if messages[-1].content.strip() != message.strip():
                # 用户改写了问题：更新原来那条，而不是再插一条
                messages[-1].content = message
                session.flush()
            history = conversation_service.history_for_prompt(session, conversation.id)
            if history and history[-1].get("role") == "user":
                history = history[:-1]
            return history
        return conversation_service.history_for_prompt(session, conversation.id)


def _record_user(conversation_id: str, message: str, mode: str) -> None:
    with session_scope() as session:
        conversation = conversation_service.get_conversation(session, conversation_id)
        conversation_service.append_message(
            session, conversation, role="user", content=message, mode=mode
        )


def _save_assistant(
    conversation_id: str,
    *,
    content: str,
    mode: str,
    citations: list[dict],
    status: str = "done",
    error_message: str | None = None,
) -> str:
    with session_scope() as session:
        conversation = conversation_service.get_conversation(session, conversation_id)
        message = conversation_service.append_message(
            session,
            conversation,
            role="assistant",
            content=content,
            mode=mode,
            citations=citations,
            status=status,
            error_message=error_message,
        )
        return message.id


def stream_events(
    message: str,
    *,
    conversation_id: str | None = None,
    mode: str = "knowledge",
    document_ids: list[str] | None = None,
    regenerate: bool = False,
) -> Iterator[Event]:
    """产出一轮问答的事件流。"""
    started = time.time()
    scope = list(document_ids or [])
    conversation_id = conversation_id or ""
    parts: list[str] = []
    citations: list[dict] = []

    try:
        conversation_id, history, mode, scope = _open_conversation(conversation_id or None, mode, scope)
        if regenerate and conversation_id:
            history = _prepare_regenerate(conversation_id, message)
        else:
            _record_user(conversation_id, message, mode)

        if mode == "general":
            messages = generation.build_general_messages(message, history)
            citations: list[dict] = []
            yield "meta", {"conversation_id": conversation_id, "mode": mode}
        else:
            query = retrieval.rewrite_query(message, history)
            if query != message:
                logger.info("问题改写：「%s」→「%s」", message[:20], query[:40])
            result = retrieval.search(query, document_ids=scope or None)
            if not result.has_evidence:
                nearby = retrieval.search_raw(message, document_ids=scope or None, top_k=3)
                citations = [item.to_dict() for item in generation.to_citations(nearby)]
                answer = generation.REFUSAL_TEXT
                message_id = _save_assistant(
                    conversation_id,
                    content=answer,
                    mode=mode,
                    citations=citations,
                    status="no_evidence",
                )
                yield "meta", {
                    "conversation_id": conversation_id,
                    "mode": mode,
                    "retrieved": 0,
                    "top_score": result.top_score,
                    "threshold": result.threshold,
                }
                yield "citations", {"citations": citations}
                yield "delta", {"text": answer}
                yield "done", {
                    "status": "no_evidence",
                    "message_id": message_id,
                    "elapsed": round(time.time() - started, 2),
                    "top_score": result.top_score,
                    "threshold": result.threshold,
                }
                return

            citations = [item.to_dict() for item in generation.to_citations(result.chunks)]
            partial = result.tier == "partial"
            messages = generation.build_knowledge_messages(
                message, result.chunks, history, partial=partial
            )
            yield "meta", {
                "conversation_id": conversation_id,
                "mode": mode,
                "retrieved": len(result.chunks),
                "top_score": result.top_score,
                "threshold": result.threshold,
            }

        for delta in llm.stream_chat(
            messages,
            temperature=generation.temperature(),
            max_tokens=generation.max_tokens("chat"),
        ):
            parts.append(delta)
            yield "delta", {"text": delta}

        answer = "".join(parts).strip()
        if citations:
            clean = generation.sanitize_citation_marks(answer, len(citations))
            if clean != answer:
                logger.warning("回答里有越界引用编号，已清理")
                answer = clean
                parts = [answer]
        if citations and answer and settings.sentence_cite_enabled:
            citations = generation.align_citations(answer, result.chunks, citations)
        if citations:
            yield "citations", {"citations": citations}

        status = "done"
        if mode == "knowledge":
            # 灰色区间即使模型选择不答，也保留 partial：界面据此提示「相关度低」，
            # 而不是把自己检索到的东西说成“完全没有依据”。
            if result.tier == "partial":
                status = "partial"
            elif generation.is_refusal(answer):
                status = "no_evidence"
        message_id = _save_assistant(
            conversation_id, content=answer, mode=mode, citations=citations, status=status
        )
        yield "done", {
            "status": status,
            "message_id": message_id,
            "elapsed": round(time.time() - started, 2),
            "answer_chars": len(answer),
            "cited": sorted(generation.cited_indexes(answer)),
        }
    except GeneratorExit:
        # 用户点了「停止」：把已生成的部分落库，标记为 stopped，不当作完整回答
        partial = "".join(parts).strip()
        if conversation_id:
            _save_assistant(
                conversation_id,
                content=partial,
                mode=mode,
                citations=citations,
                status="stopped",
                error_message="用户中止生成",
            )
        logger.info("生成被中止：已保存 %d 字（会话 %s）", len(partial), conversation_id[:8])
        raise
    except AppError as exc:
        if conversation_id:
            _save_assistant(
                conversation_id,
                content="",
                mode=mode,
                citations=[],
                status="error",
                error_message=exc.message,
            )
        logger.warning("问答失败（%s）：%s", exc.code, exc.message)
        yield "error", {"code": exc.code, "message": exc.message}
        yield "done", {"status": "error", "elapsed": round(time.time() - started, 2)}
    except Exception:  # pragma: no cover - 兜底
        logger.exception("问答未处理异常")
        yield "error", {"code": "internal_error", "message": "服务异常，请稍后重试"}
        yield "done", {"status": "error", "elapsed": round(time.time() - started, 2)}
