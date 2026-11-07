"""本地向量模型封装（BGE-small-zh）。

模型首次使用时会从 HF 镜像下载到 `DATA_DIR/models`；缓存目录与端点通过环境变量
在导入 sentence-transformers 之前设置，避免写到 C 盘用户目录。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Iterable

from app.config import settings

logger = logging.getLogger(__name__)

_model = None
_model_device = "cpu"
_model_lock = threading.Lock()


def resolve_device() -> str:
    """确定推理设备：auto 时优先 CUDA，其次 CPU。"""
    requested = (settings.embedding_device or "auto").strip().lower()
    if requested not in {"auto", ""}:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # pragma: no cover - torch 缺失时退回 CPU
        return "cpu"


def device_info() -> str:
    """健康检查用：不加载模型，只报告将使用的设备。"""
    device = resolve_device()
    if device.startswith("cuda"):
        try:
            import torch

            if torch.cuda.is_available():
                return f"cuda · {torch.cuda.get_device_name(0)}"
        except Exception:  # pragma: no cover
            pass
        return "cuda（不可用，已回退 CPU）"
    return "cpu"


def _hub_snapshot_dir():
    return settings.resolved_hf_home / "hub" / f"models--{settings.embedding_model.replace('/', '--')}"


def model_cached() -> bool:
    """本地是否已有该模型的完整快照。"""
    snapshots = _hub_snapshot_dir() / "snapshots"
    if not snapshots.is_dir():
        return False
    for snapshot in snapshots.iterdir():
        if snapshot.is_dir() and any(snapshot.iterdir()):
            return True
    return False


def prepare_hf_environment() -> None:
    """在加载模型前设置 HF 镜像、缓存路径与 CPU 线程数。

    本地已有模型时强制离线，避免每次调用都去校验版本：
    答辩环境断网、或系统代理拦截 huggingface 时仍能正常向量化。
    线程数限制为 settings.embedding_threads：默认 4，避免占满全部核心导致系统卡死。
    """
    settings.resolved_hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    os.environ["HF_HOME"] = str(settings.resolved_hf_home)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    threads = max(1, min(settings.embedding_threads, os.cpu_count() or 1))
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(key, str(threads))

    if model_cached():
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        _force_offline_constants()


def _force_offline_constants() -> None:
    """若 huggingface_hub 已在更早被导入，运行期再兜底把它切到离线。"""
    try:
        import huggingface_hub.constants as hf_constants

        hf_constants.HF_HUB_OFFLINE = True
        hf_constants.TRANSFORMERS_OFFLINE = True
    except Exception:  # pragma: no cover - 常量不存在时忽略
        pass


def get_model():
    """懒加载并缓存向量模型（单例）。有 GPU 时自动使用 CUDA。"""
    global _model, _model_device
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            prepare_hf_environment()
            import torch
            from sentence_transformers import SentenceTransformer

            device = resolve_device()
            if device.startswith("cuda") and not torch.cuda.is_available():
                logger.warning("配置要求 cuda，但当前不可用，回退 CPU")
                device = "cpu"

            if device == "cpu":
                torch.set_num_threads(max(1, min(settings.embedding_threads, os.cpu_count() or 1)))

            logger.info(
                "加载向量模型：%s（device=%s，threads=%s）",
                settings.embedding_model,
                device,
                torch.get_num_threads(),
            )
            model = SentenceTransformer(settings.embedding_model, device=device)
            if device.startswith("cuda"):
                # 半精度：显存占用约减半，速度更快，对检索精度影响很小
                model = model.half()
                logger.info("已在 GPU 上启用 fp16，显存占用约 %.0f MB", torch.cuda.memory_allocated() / 1024 / 1024)
            _model = model
            _model_device = device
            logger.info("向量模型加载完成，维度 %d，设备 %s", dimension(), device)
    return _model


def dimension() -> int:
    model = get_model()
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return int(getter())


def encode_texts(
    texts: Iterable[str],
    batch_size: int | None = None,
    progress_every: int = 200,
) -> list[list[float]]:
    """分批向量化，返回归一化向量，并按批次输出进度日志。

    手动分批而不是一次 encode 全部：既能在大文档上持续反馈进度，
    也能避免一次性构造超大数组。
    """
    items = list(texts)
    if not items:
        return []

    model = get_model()
    size = batch_size or settings.embedding_batch_size
    if _model_device.startswith("cuda"):
        size = max(size, 64)  # GPU 上增大批次更划算
    total = len(items)
    vectors: list[list[float]] = []
    last_reported = 0

    for start in range(0, total, size):
        batch = items[start : start + size]
        encoded = model.encode(
            batch,
            batch_size=len(batch),
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        vectors.extend(vector.tolist() for vector in encoded)

        done = min(start + size, total)
        if done == total or done - last_reported >= progress_every:
            logger.info("向量化进度 %d/%d", done, total)
            last_reported = done
    return vectors


def encode_query(text: str) -> list[float]:
    """查询向量。BGE 中文模型建议查询端加指令前缀以提升召回。"""
    return encode_texts([f"为这个句子生成表示以用于检索相关文章：{text}"])[0]


def is_available() -> bool:
    """健康检查用：模型是否可加载（不触发下载时可用本地缓存判断）。"""
    try:
        get_model()
        return True
    except Exception as exc:  # pragma: no cover - 依赖模型文件
        logger.warning("向量模型不可用：%s", exc)
        return False
