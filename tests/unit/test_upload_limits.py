"""入库闸门的三档判定：字数为主、页数只提示、切片数是护栏。"""

from __future__ import annotations

import pytest

from app.config import settings
from app.schemas import AppError
from app.services import ingestion


def _probe(chars: int, pages: int = 300, per_page: float | None = None) -> dict:
    return {
        "chars": chars,
        "pages": pages,
        "chars_per_page": per_page if per_page is not None else round(chars / max(1, pages), 1),
    }


def test_normal_document_passes():
    assert ingestion.check_document_limits(_probe(500_000)) == []


def test_slightly_over_limit_needs_force():
    chars = int(settings.max_document_chars * 1.2)
    with pytest.raises(AppError) as exc:
        ingestion.check_document_limits(_probe(chars))
    assert exc.value.code == "document_large_confirm"
    assert exc.value.status_code == 409
    assert "force" in exc.value.message


def test_slightly_over_limit_with_force_warns():
    chars = int(settings.max_document_chars * 1.2)
    warnings = ingestion.check_document_limits(_probe(chars), force=True)
    assert any("强制" in item for item in warnings)


def test_beyond_hard_limit_rejected():
    chars = int(settings.max_document_chars_hard * 1.01)
    with pytest.raises(AppError) as exc:
        ingestion.check_document_limits(_probe(chars), force=True)
    assert exc.value.code == "document_too_large"
    assert exc.value.status_code == 413


def test_scanned_document_rejected_by_chars_per_page():
    with pytest.raises(AppError) as exc:
        ingestion.check_document_limits(_probe(chars=3000, pages=200, per_page=15))
    assert exc.value.code == "scanned_document"
    assert "OCR" in exc.value.message


def test_clrs_sized_book_passes():
    """244 万字的千页教材（CLRS）应当直接放行，不再触发硬上限。"""
    probe = _probe(chars=2_440_000, pages=1000, per_page=2440)
    assert probe["chars"] < settings.max_document_chars
    warnings = ingestion.check_document_limits(probe)
    assert not any("强制" in item for item in warnings)


def test_chunk_guard_covers_max_chars():
    """护栏要能装下 300 万字按 442 字/片切出来的片数。"""
    assert settings.max_document_chars / 442 < settings.max_document_chunks


def test_many_pages_only_warns():
    """页数不再拦截，只提示。"""
    warnings = ingestion.check_document_limits(
        _probe(chars=settings.max_document_chars - 1, pages=settings.max_document_pages_warn + 50)
    )
    assert any("页" in item for item in warnings)
