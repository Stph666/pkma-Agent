"""知识组织测试：实体规范化、JSON 清洗、片段采样。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import enrichment


def test_normalize_entity_removes_noise():
    assert enrichment.normalize_entity("  Light RAG ") == "lightrag"
    assert enrichment.normalize_entity("《知识图谱》（上）") == "知识图谱上"
    assert enrichment.normalize_entity("Hit@5") == "hit@5"


def test_extract_json_handles_code_fence():
    raw = '```json\n{"summary": "内容", "category": "其他", "tags": ["a"]}\n```'
    assert enrichment._extract_json(raw).startswith('{"summary"')


def test_extract_json_handles_leading_text():
    raw = '好的，结果如下：{"a": 1} 希望有帮助'
    assert enrichment._extract_json(raw) == '{"a": 1}'


def _fake_chunks(count: int, size: int = 200):
    return [
        SimpleNamespace(id=f"c{i}", chunk_index=i, content="字" * size, page_start=None, page_end=None)
        for i in range(count)
    ]


def test_pick_chunks_within_budget_and_limit():
    chunks = _fake_chunks(30, size=200)
    picked = enrichment._pick_chunks(chunks, budget=1000, limit=6)

    assert len(picked) <= 6
    assert sum(len(chunk.content) for chunk in picked) <= 1000 + 200
    assert picked[0].id == "c0"


def test_pick_chunks_returns_all_when_small():
    chunks = _fake_chunks(3)
    assert len(enrichment._pick_chunks(chunks, budget=5000, limit=8)) == 3


def test_categories_are_fixed_set():
    assert "其他" in enrichment.CATEGORIES
    assert len(enrichment.CATEGORIES) == 6


def test_graph_chunk_target_scales_with_document_size():
    # 小文档全取；中大文档按比例抽样，且不小于下限、不超过上限
    assert enrichment.graph_chunk_target(1) == 1
    assert enrichment.graph_chunk_target(5) == 5
    assert enrichment.graph_chunk_target(10) == enrichment.GRAPH_MIN_CHUNKS
    assert enrichment.graph_chunk_target(1000) == 20
    assert enrichment.graph_chunk_target(2067) == 42
    # 深度档按 5% 采样（2067×5%≈104），超大文档仍受上限约束
    assert enrichment.graph_chunk_target(2067, "deep") == 104
    assert enrichment.graph_chunk_target(100000) == enrichment.DEPTH_PRESETS["standard"]["max_chunks"]
    assert enrichment.graph_chunk_target(100000, "deep") == enrichment.DEPTH_PRESETS["deep"]["max_chunks"]


def test_depth_presets_scale_chunks():
    total = 2000
    fast = enrichment.graph_chunk_target(total, "fast")
    standard = enrichment.graph_chunk_target(total, "standard")
    deep = enrichment.graph_chunk_target(total, "deep")
    assert fast < standard < deep


def test_estimate_cost_reports_calls_and_seconds():
    plan = enrichment.estimate_cost(2067, "standard")
    assert plan["chunks"] == 42
    assert plan["batches"] == 6
    assert plan["calls"] == 7
    assert plan["seconds"] > 0


def test_batch_limits_grow_with_batch_size():
    small = enrichment.batch_limits(1)
    large = enrichment.batch_limits(8)
    assert small[0] < large[0]
    assert small[1] < large[1]
    assert large[0] <= enrichment.MAX_ENTITIES_PER_BATCH
    assert large[1] <= enrichment.MAX_RELATIONS_PER_BATCH
