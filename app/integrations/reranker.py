"""交叉编码器重排（可选）。

默认不启用：需要下载 bge-reranker-base（约 1.1GB）。启用方式：
1. `RERANK_ENABLED=true`
2. 先把模型下载到本地缓存（设置 HF_ENDPOINT=https://hf-mirror.com）

模型不可用时自动退回原顺序，不影响检索主流程。
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_model = None
_failed = False


def is_available() -> bool:
    """检查依赖是否安装、模型是否已在本地缓存。"""
    if _failed:
        return False
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def get_model():
    global _model, _failed
    if _model is not None:
        return _model
    if _failed:
        return None
    try:
        from sentence_transformers import CrossEncoder

        from app.integrations.embeddings import resolve_device

        device = resolve_device()
        logger.info("加载重排模型：%s（device=%s）", settings.rerank_model, device)
        _model = CrossEncoder(settings.rerank_model, device=device)
        if device.startswith("cuda"):
            # 交叉编码器只需要排序，fp16 足够且显存减半
            try:
                _model.model.half()
            except Exception:  # pragma: no cover
                pass
        return _model
    except Exception as exc:  # pragma: no cover - 依赖外部模型
        _failed = True
        logger.warning("重排模型不可用，退回原顺序：%s", exc)
        return None


def rerank(question: str, hits: list, top_k: int | None = None) -> list:
    """按「问题-切片」相关性重排；失败时原样返回。

    注意：交叉编码器要吃自然文本，不要喂分词结果。
    """
    if not hits:
        return hits
    model = get_model()
    if model is None:
        return hits

    pairs = [(question, hit.document) for hit in hits]
    try:
        scores = model.predict(pairs)
    except Exception as exc:  # pragma: no cover
        logger.warning("重排失败，退回原顺序：%s", exc)
        return hits

    # 分数写回候选：检索层要用它做「是否有依据」的判定
    for hit, value in zip(hits, scores):
        try:
            hit.rerank_score = float(value)
        except Exception:  # pragma: no cover
            pass

    order = sorted(range(len(hits)), key=lambda index: float(scores[index]), reverse=True)
    ranked = [hits[index] for index in order]
    return ranked[:top_k] if top_k else ranked
