"""pytest 公共配置。

注意：向量库与上传目录必须在导入 app 之前指向独立临时目录。
Chroma 的本地引擎不支持多进程同时写同一目录，测试与正在运行的服务共用会损坏索引。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="pkma-test-"))
os.environ.setdefault("CHROMA_DIR", str(_TEST_ROOT / "chroma"))
os.environ.setdefault("UPLOAD_DIR", str(_TEST_ROOT / "uploads"))
# 复用项目内的模型缓存，避免测试触发重新下载
os.environ.setdefault("HF_HOME", str(ROOT / "data" / "models"))


TEST_PREFIX = "test-"
# 历史遗留：没有前缀、但确实是测试造出来的文档
LEGACY_NAMES = ("警告头用例.docx",)


def _purge_test_documents(reason: str) -> None:
    """清掉测试写入的文档（连同切片/向量/图谱）。

    没有独立测试库的原因：`.env` 里的 pkma 账号只有 `pkma.*` 的库内权限，
    没有 CREATE DATABASE，无法开 pkma_test；因此在同一库内靠前缀 + 强制清理隔离。
    """
    try:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Document
        from app.services import ingestion
    except Exception:  # pragma: no cover - 数据库不可用时跳过
        return

    try:
        with session_scope() as session:
            targets = [
                (doc.id, doc.original_name)
                for doc in session.scalars(select(Document)).all()
                if doc.original_name.startswith(TEST_PREFIX) or doc.original_name in LEGACY_NAMES
            ]
        for document_id, name in targets:
            ingestion.purge_document_artifacts(document_id)
            with session_scope() as session:
                doc = session.get(Document, document_id)
                if doc is not None:
                    session.delete(doc)
        if targets:
            print(f"[conftest] {reason}清理测试文档 {len(targets)} 份")
    except Exception as exc:  # pragma: no cover
        print(f"[conftest] 清理测试文档失败：{exc}")


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_documents():
    _purge_test_documents("会话开始前")
    yield
    _purge_test_documents("会话结束后")


@pytest.fixture(scope="session")
def tmp_settings(tmp_path_factory):
    from app.config import Settings

    data_dir = tmp_path_factory.mktemp("data")
    return Settings(data_dir=data_dir, llm_base_url="", llm_api_key="", llm_model="")


@pytest.fixture(scope="session")
def test_chroma_dir() -> Path:
    return _TEST_ROOT / "chroma"
