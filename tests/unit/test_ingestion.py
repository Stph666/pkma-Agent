"""入库校验相关单元测试（不依赖数据库）。"""

from __future__ import annotations

import pytest

from app.config import settings
from app.schemas import AppError
from app.services.ingestion import compute_sha256, validate_upload


def test_validate_upload_accepts_supported_formats():
    assert validate_upload("讲义.PDF", 1024) == ".pdf"
    assert validate_upload("笔记.docx", 1024) == ".docx"
    assert validate_upload("readme.txt", 1024) == ".txt"


def test_validate_upload_rejects_unsupported():
    with pytest.raises(AppError) as excinfo:
        validate_upload("旧文档.doc", 1024)
    assert excinfo.value.code == "unsupported_format"
    assert excinfo.value.status_code == 415


def test_validate_upload_rejects_empty():
    with pytest.raises(AppError) as excinfo:
        validate_upload("空白.txt", 0)
    assert excinfo.value.code == "empty_file"


def test_validate_upload_rejects_oversized():
    oversized = settings.max_upload_bytes + 1
    with pytest.raises(AppError) as excinfo:
        validate_upload("大文件.pdf", oversized)
    assert excinfo.value.code == "file_too_large"
    assert excinfo.value.status_code == 413


def test_validate_upload_strips_path_traversal():
    assert validate_upload("../../etc/passwd.txt", 10) == ".txt"


def test_compute_sha256_is_stable(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("内容", encoding="utf-8")
    other = tmp_path / "b.txt"
    other.write_text("内容", encoding="utf-8")
    different = tmp_path / "c.txt"
    different.write_text("另一份内容", encoding="utf-8")

    assert compute_sha256(path) == compute_sha256(other)
    assert compute_sha256(path) != compute_sha256(different)
    assert len(compute_sha256(path)) == 64
