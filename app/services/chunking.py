"""切片：把解析出的文本块按标题/段落递归切分，保留页码与重叠上下文。

约束（见实施方案 6.1）：
- 目标长度约 500 个中文字符，重叠约 80 字；
- 不切断表格行与短句，优先在句末标点处断开；
- 每块记录来源页码、切片序号与内容哈希。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.services.parsing import ParsedDocument, TextBlock

# 中文按标点切；英文句号要求后接空白，避免切坏 3.14、e.g. 这类写法
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;：:])|(?<=[.!?;])\s+(?=[A-Z0-9\"“(])")
_HEADING_LIKE = re.compile(r"^(第[一二三四五六七八九十百千0-9０-９]+[章节部分]|[0-9]+(\.[0-9]+)*[\s、.]|#+\s)")


@dataclass
class Chunk:
    chunk_index: int
    content: str
    content_hash: str
    page_start: int | None
    page_end: int | None

    @property
    def length(self) -> int:
        return len(self.content)


@dataclass
class ChunkConfig:
    chunk_size: int = 500          # char 模式下的字符预算（旧行为）
    overlap: int = 80
    unit: str = "char"             # token | char；默认保守，由 settings.chunk_unit 决定实际值
    tokens: int = 512              # token 模式下的预算，与 BGE 输入上限对齐
    overlap_tokens: int = 64

    def validate(self) -> None:
        if self.unit not in ("token", "char"):
            raise ValueError("unit 只能是 token 或 char")
        if self.unit == "token":
            if self.tokens < 64:
                raise ValueError("chunk_tokens 过小，建议不小于 64")
            if not 0 <= self.overlap_tokens < self.tokens:
                raise ValueError("overlap_tokens 必须小于 tokens")
            return
        if self.chunk_size < 100:
            raise ValueError("chunk_size 过小，建议不小于 100")
        if self.overlap < 0:
            raise ValueError("overlap 不能为负")
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap 必须小于 chunk_size")


_tokenizer = None
_token_failed = False


def _count_tokens(text: str) -> int:
    """用向量模型的 tokenizer 计数；不可用时按「中文 1 字 1 token、英文 4 字符 1 token」粗估。"""
    global _tokenizer, _token_failed
    if _tokenizer is None and not _token_failed:
        try:
            from app.integrations import embeddings

            _tokenizer = embeddings.get_model().tokenizer
        except Exception:
            _token_failed = True
    if _tokenizer is None:
        zh = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return max(1, zh + (len(text) - zh) // 4)
    return len(_tokenizer.encode(text, add_special_tokens=False))


def _split_sentences(text: str) -> list[str]:
    pieces = [piece for piece in _SENTENCE_END.split(text) if piece and piece.strip()]
    return pieces


def _chunks_by_tokens(blocks: list[TextBlock], config: ChunkConfig) -> list[Chunk]:
    """按段落累积到 token 预算：整段整段地装，装不下才按句子拆。

    关键：不能把不同段落拼进同一片。PDF 的 block 常常一行一段，
    跨段拼接会把「Docker 配置」和「Agent 定义」焊在一起，语义被稀释。
    """
    chunks: list[Chunk] = []

    def emit(text: str, page_start: int | None, page_end: int | None) -> None:
        content = text.strip()
        if not content:
            return
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=content,
                content_hash=_hash(content),
                page_start=page_start,
                page_end=page_end,
            )
        )

    buffer: list[str] = []
    buffer_tokens = 0
    page_start: int | None = None
    page_end: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_tokens, page_start, page_end
        if not buffer:
            return
        emit("\n".join(buffer), page_start, page_end)
        # 重叠：取结尾若干行（不超过 overlap 预算）
        tail: list[str] = []
        tail_tokens = 0
        for line in reversed(buffer):
            cost = _count_tokens(line)
            if tail and tail_tokens + cost > config.overlap_tokens:
                break
            tail.insert(0, line)
            tail_tokens += cost
            if tail_tokens >= config.overlap_tokens:
                break
        buffer = tail
        buffer_tokens = tail_tokens
        page_start = page_end

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        cost = _count_tokens(text)

        # 整段装不下：先落盘当前缓冲，再把这一段按句子拆
        if cost > config.tokens:
            flush()
            sentence_buffer: list[str] = []
            sentence_tokens = 0
            for sentence in _split_sentences(text):
                st = _count_tokens(sentence)
                if sentence_tokens + st > config.tokens and sentence_buffer:
                    emit("".join(sentence_buffer), block.page, block.page)
                    # 超长段落同样保留重叠，避免边界处语义断裂
                    seed: list[str] = []
                    seed_tokens = 0
                    for prev in reversed(sentence_buffer):
                        cost_prev = _count_tokens(prev)
                        if seed and seed_tokens + cost_prev > config.overlap_tokens:
                            break
                        seed.insert(0, prev)
                        seed_tokens += cost_prev
                        if seed_tokens >= config.overlap_tokens:
                            break
                    sentence_buffer, sentence_tokens = seed, seed_tokens
                sentence_buffer.append(sentence)
                sentence_tokens += st
            if sentence_buffer:
                emit("".join(sentence_buffer), block.page, block.page)
            page_start = page_end = block.page
            continue

        if buffer and buffer_tokens + cost > config.tokens:
            flush()

        buffer.append(text)
        buffer_tokens += cost
        page_start = page_start or block.page
        page_end = block.page

    if buffer:
        emit("\n".join(buffer), page_start, page_end)

    return chunks


def chunk_document_hierarchical(
    document: ParsedDocument,
    config: ChunkConfig | None = None,
    *,
    child_tokens: int = 160,
    child_overlap: int = 32,
) -> list[tuple[Chunk, list[Chunk]]]:
    """先切父块（token 预算），再把每个父块切成若干子片。

    子片用于向量检索：短、聚焦，定义句不会被无关内容稀释；
    父块随引用一起给模型：保证上下文完整。
    """
    config = config or ChunkConfig()
    config.validate()
    parents = _chunks_by_tokens(document.blocks, config)

    groups: list[tuple[Chunk, list[Chunk]]] = []
    for parent in parents:
        sentences = _split_sentences(parent.content)
        children: list[Chunk] = []
        buffer: list[str] = []
        buffer_tokens = 0

        def flush_child() -> None:
            nonlocal buffer, buffer_tokens
            content = "".join(buffer).strip()
            if content:
                children.append(
                    Chunk(
                        chunk_index=0,
                        content=content,
                        content_hash=_hash(content),
                        page_start=parent.page_start,
                        page_end=parent.page_end,
                    )
                )
            # 子片之间保留少量重叠
            tail: list[str] = []
            tail_tokens = 0
            for sentence in reversed(buffer):
                cost = _count_tokens(sentence)
                if tail and tail_tokens + cost > child_overlap:
                    break
                tail.insert(0, sentence)
                tail_tokens += cost
                if tail_tokens >= child_overlap:
                    break
            buffer = tail
            buffer_tokens = tail_tokens

        for sentence in sentences:
            cost = _count_tokens(sentence)
            if buffer and buffer_tokens + cost > child_tokens:
                flush_child()
            buffer.append(sentence)
            buffer_tokens += cost
        if buffer:
            content = "".join(buffer).strip()
            if content:
                children.append(
                    Chunk(
                        chunk_index=0,
                        content=content,
                        content_hash=_hash(content),
                        page_start=parent.page_start,
                        page_end=parent.page_end,
                    )
                )
        if not children:
            children = [
                Chunk(
                    chunk_index=0,
                    content=parent.content,
                    content_hash=parent.content_hash,
                    page_start=parent.page_start,
                    page_end=parent.page_end,
                )
            ]
        groups.append((parent, children))
    return groups


def _segments_from_block(block: TextBlock, chunk_size: int) -> list[TextBlock]:
    """把单个文本块切成不超过 chunk_size 的片段，尽量落在句末。"""
    text = block.text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [TextBlock(text=text, page=block.page, kind=block.kind)]

    pieces: list[TextBlock] = []
    buffer = ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        # 单句本身就超长：按 chunk_size 硬切，避免死循环
        while len(sentence) > chunk_size:
            if buffer:
                pieces.append(TextBlock(text=buffer, page=block.page, kind=block.kind))
                buffer = ""
            pieces.append(TextBlock(text=sentence[:chunk_size], page=block.page, kind=block.kind))
            sentence = sentence[chunk_size:]
        if len(buffer) + len(sentence) > chunk_size and buffer:
            pieces.append(TextBlock(text=buffer, page=block.page, kind=block.kind))
            buffer = sentence
        else:
            buffer += sentence
    if buffer.strip():
        pieces.append(TextBlock(text=buffer, page=block.page, kind=block.kind))
    return pieces


def _overlap_tail(text: str, overlap: int) -> str:
    """取结尾用于重叠的片段，优先对齐到句首，避免以半句话开头。"""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    for match in _SENTENCE_END.finditer(tail):
        candidate = tail[match.end() :]
        if candidate.strip():
            return candidate
    return tail


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_heading(text: str) -> bool:
    """粗判标题行：用于切片边界提示与调试。"""
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= 40 and bool(_HEADING_LIKE.match(stripped))


def chunk_document(
    document: ParsedDocument,
    config: ChunkConfig | None = None,
) -> list[Chunk]:
    """按块顺序切片，返回带页码与重叠的切片列表。"""
    config = config or ChunkConfig()
    config.validate()

    if config.unit == "token":
        return _chunks_by_tokens(document.blocks, config)

    segments: list[TextBlock] = []
    for block in document.blocks:
        segments.extend(_segments_from_block(block, config.chunk_size))
    if not segments:
        return []

    chunks: list[Chunk] = []
    buffer = ""
    page_start: int | None = None
    page_end: int | None = None

    def flush() -> str:
        nonlocal buffer, page_start, page_end
        content = buffer.strip()
        if not content:
            buffer = ""
            page_start = page_end = None
            return ""
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=content,
                content_hash=_hash(content),
                page_start=page_start,
                page_end=page_end,
            )
        )
        tail = _overlap_tail(content, config.overlap)
        buffer = tail
        page_start = page_end
        return tail

    for segment in segments:
        addition = segment.text if not buffer else f"\n{segment.text}"
        if len(buffer) + len(addition) > config.chunk_size and buffer.strip():
            flush()
            addition = segment.text if not buffer else f"\n{segment.text}"
        if page_start is None:
            page_start = segment.page
        page_end = segment.page if segment.page is not None else page_end
        buffer += addition

    if buffer.strip():
        content = buffer.strip()
        # 末尾过短的碎片并入上一块，避免产生无意义的极小切片
        if chunks and len(content) <= config.overlap // 2:
            previous = chunks[-1]
            merged = f"{previous.content}\n{content}"
            chunks[-1] = Chunk(
                chunk_index=previous.chunk_index,
                content=merged,
                content_hash=_hash(merged),
                page_start=previous.page_start,
                page_end=page_end or previous.page_end,
            )
        else:
            chunks.append(
                Chunk(
                    chunk_index=len(chunks),
                    content=content,
                    content_hash=_hash(content),
                    page_start=page_start,
                    page_end=page_end,
                )
            )

    return chunks


def chunk_text(text: str, config: ChunkConfig | None = None) -> list[Chunk]:
    """纯文本切片的便捷入口，供测试与脚本使用。"""
    block = TextBlock(text=text)
    return chunk_document(ParsedDocument(blocks=[block], char_count=len(text)), config)
