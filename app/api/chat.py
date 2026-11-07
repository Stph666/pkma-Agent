"""问答接口：把服务层事件流编码为 SSE（meta / delta / citations / done / error）。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.config import settings
from app.schemas import AppError, ChatRequest
from app.services import qa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _encode(request: ChatRequest):
    for event, payload in qa.stream_events(
        request.message,
        conversation_id=request.conversation_id,
        mode=request.mode,
        document_ids=request.document_ids,
        regenerate=request.regenerate,
    ):
        yield _sse(event, payload)


@router.post("/stream", summary="流式问答（知识库 / 通用）")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    if not settings.llm_configured:
        raise AppError("llm_not_configured", "未配置生成模型，请在 .env 填写 LLM 相关配置", status_code=503)
    return StreamingResponse(
        _encode(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
