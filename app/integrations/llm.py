"""OpenAI 兼容的 LLM 客户端。

支持会返回 `reasoning_content` 的推理模型（如 deepseek-flash）：
推理内容只用于日志统计，不混入正文，也不返回给前端。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import httpx

from app.config import settings
from app.schemas import AppError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def is_configured() -> bool:
    return settings.llm_configured


def _endpoint() -> str:
    return f"{settings.llm_base_url.rstrip('/')}/chat/completions"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }


def _payload(
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    stream: bool,
    json_mode: bool,
) -> dict:
    body: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
        # 推理模型会先消耗 token 思考，预算由调用方按用途显式给出
        "max_tokens": max_tokens or settings.llm_max_tokens_chat,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if settings.llm_thinking == "disabled":
        # 关闭推理模型的思考阶段：首字延迟从十几秒降到两秒内
        body["thinking"] = {"type": "disabled"}
    return body


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.llm_timeout_seconds, connect=10.0)


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> str:
    """非流式对话，返回正文。失败时抛 AppError。"""
    if not is_configured():
        raise AppError("llm_not_configured", "未配置生成模型，请在 .env 填写 LLM 相关配置", status_code=503)

    body = _payload(messages, temperature, max_tokens, stream=False, json_mode=json_mode)
    last_error: Exception | None = None
    for attempt in (1, 2):
        started = time.time()
        try:
            response = httpx.post(_endpoint(), headers=_headers(), json=body, timeout=_timeout())
        except httpx.TimeoutException as exc:
            last_error = exc
            logger.warning("LLM 请求超时（第 %d 次）", attempt)
            continue
        except httpx.HTTPError as exc:
            raise AppError("llm_unavailable", "无法连接模型服务，请检查网络或接口地址", status_code=503) from exc

        if response.status_code in RETRYABLE_STATUS and attempt == 1:
            logger.warning("LLM 返回 %s，重试一次", response.status_code)
            time.sleep(1.5)
            continue
        if response.status_code != 200:
            logger.error("LLM 调用失败：%s %s", response.status_code, response.text[:200])
            raise AppError(
                "llm_error",
                f"模型服务返回异常（{response.status_code}）",
                status_code=502 if response.status_code >= 500 else 400,
            )

        data = response.json()
        message = data["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        reasoning = message.get("reasoning_content") or ""
        usage = data.get("usage", {})
        logger.info(
            "LLM 完成：%.1fs，推理 %d 字，正文 %d 字，tokens=%s",
            time.time() - started,
            len(reasoning),
            len(content),
            usage.get("completion_tokens"),
        )
        if not content:
            raise AppError("llm_empty_answer", "模型没有返回有效内容，请重试", status_code=502)
        return content

    raise AppError("llm_timeout", "模型响应超时，请稍后重试", status_code=504) from last_error


def stream_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> Iterator[str]:
    """流式对话，逐段产出正文；推理内容丢弃。"""
    if not is_configured():
        raise AppError("llm_not_configured", "未配置生成模型，请在 .env 填写 LLM 相关配置", status_code=503)

    body = _payload(messages, temperature, max_tokens, stream=True, json_mode=False)
    started = time.time()
    first_token_at: float | None = None
    collected = 0

    try:
        with httpx.stream("POST", _endpoint(), headers=_headers(), json=body, timeout=_timeout()) as response:
            if response.status_code != 200:
                detail = response.read()[:200].decode("utf-8", "ignore")
                logger.error("LLM 流式调用失败：%s %s", response.status_code, detail)
                raise AppError("llm_error", f"模型服务返回异常（{response.status_code}）", status_code=502)

            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    payload = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if not text:
                    continue
                if first_token_at is None:
                    first_token_at = time.time() - started
                collected += len(text)
                yield text
    except httpx.TimeoutException as exc:
        raise AppError("llm_timeout", "模型响应超时，请稍后重试", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise AppError("llm_unavailable", "无法连接模型服务，请检查网络或接口地址", status_code=503) from exc

    logger.info(
        "LLM 流式完成：首字 %.2fs，总计 %.1fs，正文 %d 字",
        first_token_at or -1,
        time.time() - started,
        collected,
    )
    if collected == 0:
        raise AppError("llm_empty_answer", "模型没有返回有效内容，请重试", status_code=502)
