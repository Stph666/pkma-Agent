"""生成层测试：上下文编号、引用映射、拒答识别、提示词组装。"""

from __future__ import annotations

from app.services import generation
from app.services.retrieval import RetrievedChunk


def _chunk(index: int, content: str = "片段内容" * 20, page: int | None = 3) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"c{index}",
        document_id="doc-1",
        filename="资料.pdf",
        chunk_index=index,
        page_start=page,
        page_end=page,
        content=content,
        score=0.61,
    )


def test_build_context_numbers_sources():
    context = generation.build_context([_chunk(0), _chunk(1, page=None)])
    assert context.startswith("【1】来源：资料.pdf（第 3 页）")
    assert "【2】来源：资料.pdf（片段 #1）" in context


def test_to_citations_maps_fields_and_truncates():
    long_text = "很长的内容" * 100
    citations = generation.to_citations([_chunk(0, content=long_text)], snippet_length=50)

    assert citations[0].index == 1
    assert citations[0].filename == "资料.pdf"
    assert citations[0].location == "第 3 页"
    assert citations[0].snippet.endswith("…")
    assert len(citations[0].snippet) == 51


def test_cited_indexes_parses_brackets():
    answer = "结论一 [1]，结论二 [2]，重复引用 [1]，越界 [12]。"
    assert generation.cited_indexes(answer) == {1, 2, 12}
    assert generation.cited_indexes("没有引用") == set()


def test_is_refusal():
    assert generation.is_refusal(generation.REFUSAL_TEXT)
    assert not generation.is_refusal("检索增强生成是先检索再生成的技术 [1]。")


def test_knowledge_messages_carry_context_and_limit_history():
    history = [{"role": "user", "content": "上一轮问题"}, {"role": "assistant", "content": "上一轮回答"}]
    messages = generation.build_knowledge_messages("本轮问题", [_chunk(0)], history)

    assert messages[0]["role"] == "system"
    assert "编号片段" in messages[-1]["content"]
    assert "本轮问题" in messages[-1]["content"]
    # 知识库模式最多带两条历史，避免历史覆盖证据
    assert len(messages) <= 4


def test_general_messages_without_context():
    messages = generation.build_general_messages("随便聊聊", [{"role": "user", "content": "你好"}])
    assert messages[0]["content"] == generation.SYSTEM_GENERAL
    assert messages[-1] == {"role": "user", "content": "随便聊聊"}
    assert all("编号片段" not in item["content"] for item in messages)


def test_refusal_only_when_leading():
    """正文中途提到“没有足够依据”不算拒答。"""
    answer = "根据片段 [1]，增量更新需要清理旧向量。若片段不足会提示没有足够依据。"
    assert generation.is_refusal(answer) is False
