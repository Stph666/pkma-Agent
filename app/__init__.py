"""应用包入口。

在这里最先设置 HuggingFace 相关环境变量：`huggingface_hub` 会在导入时读取
这些常量，若等到加载模型时再设置就来不及了。
"""

from __future__ import annotations

import os
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_HF_HOME = os.environ.get("HF_HOME") or str(_BASE_DIR / "data" / "models")
_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

os.environ.setdefault("HF_HOME", _HF_HOME)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_snapshots = Path(_HF_HOME) / "hub" / f"models--{_MODEL_NAME.replace('/', '--')}" / "snapshots"
if _snapshots.is_dir() and any(snapshot.is_dir() and any(snapshot.iterdir()) for snapshot in _snapshots.iterdir()):
    # 本地已有模型：强制离线，避免系统代理或断网导致向量化失败
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
