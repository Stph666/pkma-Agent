"""生成：提示词组装、引用格式化与拒答语义。

知识库模式要求模型只依据编号片段作答，并用 [1][2] 标注来源；
无充分依据时返回固定拒答语义，由界面引导用户改用通用知识。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import settings
from app.services.retrieval import RetrievedChunk

REFUSAL_TEXT = "当前知识库中没有足够依据回答这个问题。以下是最相关的原文片段，你可以判断是否需要改用通用知识回答。"

SYSTEM_KNOWLEDGE = (
    "你是个人知识库的写作助手。回答要像给同事讲清楚一件事，而不是罗列检索结果。\n"
    "写作要求：\n"
    "1. 结论先行：第一句直接回答用户的问题，不要用“根据片段”“本文介绍了”开头。\n"
    "2. 因果串联：说清为什么，用“因为……所以……”“这样做的好处是……”把前后串起来，不要只堆事实。\n"
    "3. 分点要写完整句：需要分点时，每点是一句完整的话，能独立读懂，不要只写名词短语。\n"
    "4. 术语保留原文：专有名词、模型名、指标名保持原文（如 RAG、Hit@5、Transformer），不要翻译成中文别称。\n"
    "5. 引用规则：每个结论后用 [1]、[2] 标注来源，编号必须与片段编号一致，不得编造编号。\n"
    "6. 只用【编号片段】里的事实，不得引入片段之外的知识。历史消息只用于理解指代关系，"
    "结论必须来自本轮片段。\n"
    f"7. 片段不足以回答时，只输出这一句：「{REFUSAL_TEXT}」"
    "不要补充解释，不要标注引用编号。"
)

_general_prompt_override: str = ""


def current_general_prompt() -> str:
    """界面里改过的通用提示词优先，未改则用内置默认。"""
    return (_general_prompt_override or "").strip() or SYSTEM_GENERAL


def set_general_prompt(text: str) -> str:
    """保存通用对话提示词（运行期生效，重启回到默认）。"""
    global _general_prompt_override
    _general_prompt_override = (text or "").strip()
    return current_general_prompt()


SYSTEM_GENERAL = (
    "你是一个友好的通用 AI 助手。当前对话没有使用知识库，请基于通用知识回答。\n"
    "如果回答依赖时效性很强或不确定的信息，请明确说明。用中文简洁作答。"
)

_CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")


@dataclass
class Citation:
    index: int
    chunk_id: str
    document_id: str
    filename: str
    location: str
    page_start: int | None
    page_end: int | None
    score: float
    snippet: str
    origin: str = "vector"
    content: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "filename": self.filename,
            "location": self.location,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "score": self.score,
            "snippet": self.snippet,
            "origin": self.origin,
            "content": self.content,
        }


def build_context(chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        blocks.append(f"【{index}】来源：{chunk.filename}（{chunk.location}）\n{chunk.content}")
    return "\n\n".join(blocks)



SENTENCE_END = "。！？；\n"


def split_sentences(text: str, min_chars: int = 8) -> list[str]:
    """按中文句末标点切句，太短的碎片并入下一句。"""
    if not text:
        return []
    pieces: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in SENTENCE_END:
            if buffer.strip():
                pieces.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        pieces.append(buffer.strip())

    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    return merged


def align_citations(
    answer: str,
    chunks: list[RetrievedChunk],
    citations: list[dict],
) -> list[dict]:
    """给每条引用补一个「命中句」：答案里引用它的句子，与切片内最相似的句子。"""
    if not citations or not answer:
        return citations

    from app.integrations import embeddings

    answer_sentences = split_sentences(answer)
    by_index = {item["index"]: item for item in citations}

    # 每条引用对应答案里提到它的句子
    citing: dict[int, list[str]] = {}
    for sentence in answer_sentences:
        for index in by_index:
            if f"[{index}]" in sentence:
                citing.setdefault(index, []).append(sentence)
    if not citing:
        return citations

    chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    try:
        for index, sentences in citing.items():
            item = by_index[index]
            chunk = chunk_by_id.get(item.get("chunk_id"))
            if chunk is None:
                continue
            chunk_sentences = split_sentences(chunk.content)[:14]
            if not chunk_sentences:
                continue
            vectors = embeddings.encode_texts(sentences + chunk_sentences)
            query_vectors = vectors[: len(sentences)]
            doc_vectors = vectors[len(sentences) :]

            best_score = -1.0
            best_sentence = chunk_sentences[0]
            for query_vector in query_vectors:
                for doc_vector, doc_sentence in zip(doc_vectors, chunk_sentences):
                    score = sum(a * b for a, b in zip(query_vector, doc_vector))
                    if score > best_score:
                        best_score, best_sentence = score, doc_sentence
            item["quote"] = best_sentence[:160]
            item["quote_score"] = round(best_score, 3)
            # 答案里引用这条来源的原句：读者不用回头找哪句话用了它
            item["supports"] = " ".join(sentences)[:220]
    except Exception:
        # 对齐失败不影响回答，退回整段引用
        return citations
    return citations


def to_citations(chunks: list[RetrievedChunk], snippet_length: int = 160) -> list[Citation]:
    citations = []
    for index, chunk in enumerate(chunks, start=1):
        # 父块给模型看，引用里优先展示「命中的子片」，读者一眼能对上句子
        source = chunk.highlight or chunk.content
        snippet = source[:snippet_length].replace("\n", " ")
        if len(chunk.content) > snippet_length:
            snippet += "…"
        citations.append(
            Citation(
                index=index,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                filename=chunk.filename,
                location=chunk.location,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                score=chunk.score,
                snippet=snippet,
                origin=chunk.origin,
                content=chunk.content[:1500],
            )
        )
    return citations


def _history_messages(history: list[dict], limit: int, max_chars: int) -> list[dict[str, str]]:
    """按条数与字符数双重截断，避免历史覆盖当前证据。"""
    if not history:
        return []
    picked: list[dict[str, str]] = []
    used = 0
    for message in reversed(history[-limit:]):
        text = (message.get("content") or "").strip()
        if not text:
            continue
        if used + len(text) > max_chars:
            break
        picked.append({"role": message["role"], "content": text})
        used += len(text)
    return list(reversed(picked))


PARTIAL_CAVEAT = "注意：本次检索到的片段与问题相关度一般，只能回答其中有依据的部分。"


def build_knowledge_messages(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[dict] | None = None,
    *,
    partial: bool = False,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_KNOWLEDGE}]
    # 知识库模式只带最近两条历史，且总长度受限
    # 放宽到 6 条 / 2000 字，仍做上限截断，避免历史覆盖本轮证据
    messages.extend(_history_messages(history or [], limit=6, max_chars=2000))
    instruction = "请只依据上面的片段作答，并在结论后用 [编号] 标注来源。"
    if partial:
        instruction += (
            "\n片段可能只覆盖问题的一部分：能回答的部分直接回答并标注来源；"
            "片段没有覆盖的部分，明确说一句「这一点资料里没有提到」，不要猜测。"
        )
    messages.append(
        {
            "role": "user",
            "content": (
                f"【编号片段】\n{build_context(chunks)}\n\n"
                f"【问题】\n{question}\n\n"
                f"{instruction}"
            ),
        }
    )
    return messages


def build_general_messages(
    question: str,
    history: list[dict] | None = None,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": current_general_prompt()}]
    messages.extend(_history_messages(history or [], limit=10, max_chars=4000))
    messages.append({"role": "user", "content": question})
    return messages


def cited_indexes(answer: str) -> set[int]:
    """从答案中提取引用编号，用于引用准确率与越界检查。"""
    return {int(match) for match in _CITATION_PATTERN.findall(answer or "")}


REFUSAL_MARKER = "没有足够依据"
REFUSAL_PREFIXES = ("当前知识库中没有足够依据", "知识库中没有足够依据")


CITE_MARK = re.compile(r"\[(\d{1,3})\]")


def sanitize_citation_marks(answer: str, total: int) -> str:
    """去掉越界的引用编号：模型有时会写 [7]，实际只有 5 条来源。"""
    if not answer:
        return answer

    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return match.group(0) if 1 <= index <= total else ""

    return CITE_MARK.sub(_replace, answer)


def is_refusal(answer: str) -> bool:
    """只把以固定拒答句开头的回答判为无依据；正文里提到该词不算。"""
    text = (answer or "").strip().lstrip("注意:：，,。 ")
    return text.startswith(REFUSAL_PREFIXES)


def temperature(mode: str = "knowledge") -> float:
    """对话温度：知识库与通用都用 chat 档，抽取走更低的 extract 档。"""
    return settings.llm_temperature_chat


def max_tokens(mode: str = "chat") -> int:
    return settings.llm_max_tokens_chat if mode == "chat" else settings.llm_max_tokens_extract
