"""检索层测试：阈值过滤、去重、上下文数量与无依据判定。"""

from __future__ import annotations

import pytest

from app.integrations.vector_store import VectorHit
from app.services import retrieval


def _hit(identifier: str, score: float, chunk_index: int = 0) -> VectorHit:
    return VectorHit(
        id=identifier,
        document="片段内容",
        metadata={
            "document_id": "doc-1",
            "filename": "资料.pdf",
            "chunk_index": chunk_index,
            "page_start": 2,
            "page_end": 2,
        },
        distance=round(1 - score, 4),
    )


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "encode_query", lambda text: [0.1] * 8)
    # 这些用例验证的是「向量阈值」这套策略；开了交叉编码器后档位改由重排分数决定，
    # 与它们的前提冲突，所以单测里显式关掉重排（生产默认开启）。
    from app.config import settings

    monkeypatch.setattr(settings, "rerank_enabled", False)


def test_filters_by_threshold(monkeypatch):
    monkeypatch.setattr(
        retrieval.vector_store,
        "query",
        lambda vector, top_k, document_ids=None: [_hit("a", 0.62), _hit("b", 0.31)],
    )

    result = retrieval.search("问题", threshold=0.45)
    assert [chunk.chunk_id for chunk in result.chunks] == ["a"]
    assert result.has_evidence is True
    assert result.top_score == 0.62


def test_no_evidence_when_all_below_partial_floor(monkeypatch):
    monkeypatch.setattr(
        retrieval.vector_store,
        "query",
        lambda vector, top_k, document_ids=None: [_hit("a", 0.30), _hit("b", 0.25)],
    )

    result = retrieval.search("无关问题", threshold=0.45)
    assert result.chunks == []
    assert result.has_evidence is False
    assert result.tier == "none"


def test_gray_zone_returns_partial(monkeypatch):
    """低于阈值但高于灰色下限：给「部分回答」的候选，而不是直接拒答。"""
    monkeypatch.setattr(
        retrieval.vector_store,
        "query",
        lambda vector, top_k, document_ids=None: [_hit("a", 0.40), _hit("b", 0.38)],
    )

    result = retrieval.search("边界问题", threshold=0.45)
    assert result.tier == "partial"
    assert result.chunks, "灰色区间应返回候选片段"
    assert result.has_evidence is True


def test_deduplicates_and_caps_context(monkeypatch):
    hits = [_hit("a", 0.9), _hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7), _hit("d", 0.6)]
    monkeypatch.setattr(retrieval.vector_store, "query", lambda vector, top_k, document_ids=None: hits)

    result = retrieval.search("问题", threshold=0.1, context_k=2)
    assert [chunk.chunk_id for chunk in result.chunks] == ["a", "b"]


def test_document_scope_forwarded(monkeypatch):
    captured = {}

    def fake_query(vector, top_k, document_ids=None):
        captured["document_ids"] = document_ids
        return [_hit("a", 0.9)]

    monkeypatch.setattr(retrieval.vector_store, "query", fake_query)
    retrieval.search("问题", document_ids=["doc-9"], threshold=0.1)
    assert captured["document_ids"] == ["doc-9"]


def test_empty_question_returns_no_evidence():
    result = retrieval.search("   ")
    assert result.chunks == []
    assert result.has_evidence is False


def test_location_formatting():
    chunk = retrieval.RetrievedChunk(
        chunk_id="c",
        document_id="d",
        filename="f.pdf",
        chunk_index=4,
        page_start=3,
        page_end=5,
        content="x",
        score=0.5,
    )
    assert chunk.location == "第 3–5 页"
    assert retrieval.RetrievedChunk(
        chunk_id="c", document_id="d", filename="f.txt", chunk_index=4,
        page_start=None, page_end=None, content="x", score=0.5,
    ).location == "片段 #4"


def test_graph_expansion_is_noop_for_baseline():
    result = retrieval.RetrievalResult(chunks=[], top_score=0.1, threshold=0.45)
    assert retrieval.expand_with_graph("问题", result) is result


def _node(name: str, normalized: str | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(normalized_name=normalized or name.lower(), name=name)


def test_entity_matching_uses_aliases():
    nodes = [_node("检索增强生成", "检索增强生成"), _node("Transformer", "transformer")]
    matched = retrieval._match_entities("RAG 和 Transformer 有什么关系？", nodes)
    names = [node.name for node in matched]
    assert "检索增强生成" in names  # 通过别名 rag 命中
    assert len(matched) <= retrieval.MAX_MATCHED_ENTITIES


def test_entity_matching_prefers_longer_names():
    nodes = [_node("向量", "向量"), _node("向量数据库", "向量数据库"), _node("Transformer", "transformer")]
    matched = retrieval._match_entities("向量数据库怎么选？", nodes)
    assert [node.name for node in matched] == ["向量数据库"]


def test_entity_matching_limits_to_two():
    nodes = [_node(n, n.lower()) for n in ["检索", "切片", "微调", "向量"]]
    matched = retrieval._match_entities("检索、切片、微调和向量", nodes)
    assert len(matched) == retrieval.MAX_MATCHED_ENTITIES
