"""切片测试：长度、重叠、顺序、页码与哈希稳定性。"""

from __future__ import annotations

import pytest

from app.services.chunking import ChunkConfig, chunk_document, chunk_text, is_heading
from app.services.parsing import ParsedDocument, TextBlock

LONG_TEXT = "。".join(f"这是第{i}句用于测试切片逻辑的正文内容，需要足够长才能触发切分" for i in range(1, 31)) + "。"


def test_short_text_single_chunk():
    chunks = chunk_text("只有一句话。")
    assert len(chunks) == 1
    assert chunks[0].content == "只有一句话。"
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_start is None


def test_long_text_is_split_with_continuous_indexes():
    chunks = chunk_text(LONG_TEXT, ChunkConfig(chunk_size=200, overlap=40))

    assert len(chunks) > 1
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.length <= 260 for chunk in chunks)


def test_adjacent_chunks_share_overlap():
    chunks = chunk_text(LONG_TEXT, ChunkConfig(chunk_size=200, overlap=40))

    overlap_count = 0
    for previous, current in zip(chunks, chunks[1:]):
        tail = previous.content[-20:]
        if tail and tail in current.content:
            overlap_count += 1
    assert overlap_count >= len(chunks) - 2


def test_no_empty_or_duplicate_chunks():
    chunks = chunk_text(LONG_TEXT, ChunkConfig(chunk_size=150, overlap=30))
    contents = [chunk.content for chunk in chunks]

    assert all(content.strip() for content in contents)
    assert len(set(chunk.content_hash for chunk in chunks)) == len(chunks)


def test_pdf_pages_propagate_to_chunks():
    document = ParsedDocument(
        blocks=[
            TextBlock(text="第一页内容" * 30, page=1),
            TextBlock(text="第二页内容" * 30, page=2),
        ],
        page_count=2,
        char_count=300,
    )
    chunks = chunk_document(document, ChunkConfig(chunk_size=120, overlap=20))

    assert chunks[0].page_start == 1
    assert any(chunk.page_end == 2 for chunk in chunks)
    assert all(chunk.page_start is not None for chunk in chunks)


def test_hash_is_stable_for_same_content():
    first = chunk_text("重复内容用于校验哈希。")
    second = chunk_text("重复内容用于校验哈希。")

    assert first[0].content_hash == second[0].content_hash


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        ChunkConfig(chunk_size=100, overlap=100).validate()
    with pytest.raises(ValueError):
        ChunkConfig(chunk_size=50, overlap=10).validate()
    with pytest.raises(ValueError):
        ChunkConfig(chunk_size=300, overlap=-1).validate()


def test_empty_document_returns_no_chunks():
    assert chunk_document(ParsedDocument()) == []


def test_heading_detection():
    assert is_heading("第3章 检索增强")
    assert is_heading("2.1 切片策略")
    assert is_heading("## 数据设计")
    assert not is_heading("这是一段普通的正文内容，不属于标题。")
