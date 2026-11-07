"""token 切片：预算、重叠、英文断句。"""

from __future__ import annotations

from app.services.chunking import ChunkConfig, _count_tokens, chunk_document
from app.services.parsing import ParsedDocument, TextBlock


def _document(text: str) -> ParsedDocument:
    return ParsedDocument(blocks=[TextBlock(text=text, page=1)], page_count=1, char_count=len(text))


def test_token_chunks_respect_budget():
    text = " ".join(["Retrieval augmented generation combines search with generation."] * 200)
    chunks = chunk_document(_document(text), ChunkConfig(unit="token", tokens=200, overlap_tokens=20))
    assert len(chunks) > 1
    assert all(_count_tokens(chunk.content) <= 230 for chunk in chunks)


def test_overlap_is_carried():
    text = " ".join([f"Sentence number {i} talks about knowledge management." for i in range(120)])
    chunks = chunk_document(_document(text), ChunkConfig(unit="token", tokens=120, overlap_tokens=30))
    assert len(chunks) > 2
    assert chunks[0].content[-40:] in chunks[1].content


def test_english_period_splits_sentences():
    """英文句号必须能断句，否则整段会被硬切。"""
    from app.services.chunking import _split_sentences

    pieces = _split_sentences("First sentence. Second sentence! Third one?")
    assert len(pieces) == 3


def test_char_mode_still_works():
    text = "知识管理。" * 200
    chunks = chunk_document(_document(text), ChunkConfig(unit="char", chunk_size=300, overlap=50))
    # 旧行为允许「重叠尾巴 + 新片段」略超预算，容忍一个 overlap
    assert chunks and all(len(chunk.content) <= 300 + 50 for chunk in chunks)
