"""问答流程集成测试：用假的生成模型验证事件顺序、落库与拒答。

不调用真实 LLM，保证测试可离线、可重复、无费用。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import check_database
from app.services import generation, qa, retrieval
from app.services.retrieval import RetrievedChunk

DB_OK, DB_DETAIL = check_database()
pytestmark = pytest.mark.skipif(not DB_OK, reason=f"MySQL 不可用：{DB_DETAIL}")


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fake_llm(monkeypatch):
    """默认让模型回一句带引用的答案。"""
    monkeypatch.setattr(
        qa.llm,
        "stream_chat",
        lambda messages, temperature=0.3, max_tokens=None: iter(["切片过短会丢失上下文", "，过长会引入噪声 [1]。"]),
    )


@pytest.fixture()
def fake_retrieval(monkeypatch):
    chunk = RetrievedChunk(
        chunk_id="c1",
        document_id="doc-1",
        filename="资料.pdf",
        chunk_index=0,
        page_start=1,
        page_end=2,
        content="切片过短会丢失上下文，过长会引入噪声。",
        score=0.61,
    )
    monkeypatch.setattr(
        qa.retrieval,
        "search",
        lambda message, document_ids=None: retrieval.RetrievalResult(
            chunks=[chunk], top_score=0.61, threshold=0.45
        ),
    )
    return chunk


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = None
    for line in raw.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.append((event, json.loads(line[5:])))
    return events


def _ask(
    client: TestClient,
    message: str,
    mode: str = "knowledge",
    conversation_id: str | None = None,
    *,
    regenerate: bool = False,
):
    payload = {"message": message, "mode": mode}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if regenerate:
        payload["regenerate"] = True
    with client.stream("POST", "/api/chat/stream", json=payload) as response:
        assert response.status_code == 200
        return _parse_sse("".join(response.iter_text()))


def test_knowledge_answer_streams_with_citations(client: TestClient, fake_llm, fake_retrieval):
    events = _ask(client, "切片长度对召回有什么影响？")
    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert "delta" in names
    assert "citations" in names
    assert names[-1] == "done"

    deltas = "".join(payload["text"] for name, payload in events if name == "delta")
    assert "丢失上下文" in deltas

    citations = next(payload["citations"] for name, payload in events if name == "citations")
    assert citations[0]["filename"] == "资料.pdf"
    assert citations[0]["index"] == 1

    done = events[-1][1]
    assert done["status"] == "done"
    assert done["cited"] == [1]


def test_answer_is_persisted_and_reloadable(client: TestClient, fake_llm, fake_retrieval):
    events = _ask(client, "增量更新要清理什么？")
    conversation_id = next(payload["conversation_id"] for name, payload in events if name == "meta")

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "增量更新要清理什么？"
    assert messages[1]["citations"]

    conversations = client.get("/api/conversations").json()["items"]
    assert any(item["id"] == conversation_id for item in conversations)
    client.delete(f"/api/conversations/{conversation_id}")


def test_model_refusal_marked_as_no_evidence(client: TestClient, fake_retrieval, monkeypatch):
    monkeypatch.setattr(
        qa.llm,
        "stream_chat",
        lambda messages, temperature=0.3, max_tokens=None: iter(["当前知识库中没有足够依据回答这个问题。"]),
    )
    events = _ask(client, "怎么写快速排序？")
    assert events[-1][1]["status"] == "no_evidence"


def test_low_score_skips_generation(client: TestClient, monkeypatch):
    calls = {"llm": 0}

    def fail_if_called(messages, temperature=0.3, max_tokens=None):
        calls["llm"] += 1
        yield "不应该被调用"

    monkeypatch.setattr(qa.llm, "stream_chat", fail_if_called)
    monkeypatch.setattr(
        qa.retrieval,
        "search",
        lambda message, document_ids=None: retrieval.RetrievalResult(chunks=[], top_score=0.31, threshold=0.45),
    )
    monkeypatch.setattr(qa.retrieval, "search_raw", lambda message, document_ids=None, top_k=3: [])

    events = _ask(client, "今天天气怎么样？")
    assert calls["llm"] == 0
    answer = "".join(payload["text"] for name, payload in events if name == "delta")
    assert answer == generation.REFUSAL_TEXT
    assert events[-1][1]["status"] == "no_evidence"


def test_general_mode_has_no_citations(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        qa.llm,
        "stream_chat",
        lambda messages, temperature=0.3, max_tokens=None: iter(["这是通用知识回答。"]),
    )
    events = _ask(client, "解释一下什么是向量", mode="general")
    assert not any(name == "citations" for name, _ in events)
    assert events[-1][1]["status"] == "done"

    conversation_id = next(payload["conversation_id"] for name, payload in events if name == "meta")
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert messages[-1]["citations"] == []
    client.delete(f"/api/conversations/{conversation_id}")


def test_conversation_title_comes_from_first_question(client: TestClient, fake_llm, fake_retrieval):
    events = _ask(client, "知识图谱里的三元组是什么？")
    conversation_id = next(payload["conversation_id"] for name, payload in events if name == "meta")
    conversations = client.get("/api/conversations").json()["items"]
    item = next(entry for entry in conversations if entry["id"] == conversation_id)
    assert item["title"].startswith("知识图谱里的三元组")
    client.delete(f"/api/conversations/{conversation_id}")


def test_stopped_generation_saves_partial(client: TestClient, fake_retrieval, monkeypatch):
    """用户点停止时，已生成的部分要落库为 stopped，而不是丢掉或当成完整回答。"""
    from app.services import conversations as conversation_service
    from app.db import session_scope

    def slow_stream(messages, temperature=0.3, max_tokens=None):
        for piece in ["第一段。", "第二段。", "第三段。", "第四段。"]:
            yield piece

    monkeypatch.setattr(qa.llm, "stream_chat", slow_stream)

    events = qa.stream_events("切片怎么切？", mode="knowledge")
    conversation_id = ""
    for index, (name, payload) in enumerate(events):
        if name == "meta":
            conversation_id = payload["conversation_id"]
        if index >= 3:
            break
    events.close()  # 模拟前端取消

    with session_scope() as session:
        messages = conversation_service.list_messages(session, conversation_id)

    assistant = [m for m in messages if m.role == "assistant"]
    assert assistant, "中止后应保存已生成内容"
    assert assistant[-1].status == "stopped"
    assert len(assistant[-1].content) > 0
    client.delete(f"/api/conversations/{conversation_id}")


def test_regenerate_replaces_instead_of_appending(client: TestClient, fake_llm, fake_retrieval):
    """重新生成必须走真实接口参数，而不是手工删一条再问一遍。"""
    events = _ask(client, "增量更新要注意什么？")
    conversation_id = next(payload["conversation_id"] for name, payload in events if name == "meta")

    again = _ask(client, "增量更新要注意什么？", conversation_id=conversation_id, regenerate=True)
    assert again[-1][1]["status"] == "done"

    from app.db import session_scope
    from app.services import conversations as conversation_service

    with session_scope() as session:
        after = conversation_service.list_messages(session, conversation_id)
    # 重新生成后仍是 user / assistant 两条，不能多出一条重复的 user
    assert [m.role for m in after] == ["user", "assistant"]
    assert after[0].content == "增量更新要注意什么？"
    client.delete(f"/api/conversations/{conversation_id}")
