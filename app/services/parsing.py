"""文档解析：PDF、DOCX、TXT 统一转为带来源位置的文本块。

解析只做格式识别与文本提取，不改变原文语义；来源页码/位置信息供引用溯源使用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.schemas import AppError

BlockKind = Literal["paragraph", "table"]

_CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class TextBlock:
    """解析得到的最小文本单元。page 为 1 起页码，TXT 为 None。"""

    text: str
    page: int | None = None
    kind: BlockKind = "paragraph"


@dataclass
class ParsedDocument:
    blocks: list[TextBlock] = field(default_factory=list)
    page_count: int = 0
    char_count: int = 0

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text)


def normalize_text(text: str) -> str:
    """清理不可见字符与多余空白，但不改写用词与标点。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _split_paragraphs(text: str, max_len: int = 1200) -> list[str]:
    """按空行/换行切段；过长的单行再按句末标点切开。"""
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        piece = raw.strip()
        if not piece:
            continue
        if len(piece) <= max_len:
            paragraphs.append(piece)
            continue
        buffer = ""
        for sentence in re.split(r"(?<=[。！？；!?;])", piece):
            if len(buffer) + len(sentence) > max_len and buffer:
                paragraphs.append(buffer.strip())
                buffer = sentence
            else:
                buffer += sentence
        if buffer.strip():
            paragraphs.append(buffer.strip())
    return paragraphs



# 页眉页脚：出现在大量页面上的短行（如书名、章节名）
HEADER_MIN_PAGES_RATIO = 0.2
HEADER_MAX_CHARS = 40
HEADER_MIN_PAGES = 5
# 目录行：右侧带点线引导 + 页码
TOC_PATTERN = re.compile(r"([.．·…]{3,}|\s{4,})\s*\d{1,4}\s*$")


def _is_toc_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if TOC_PATTERN.search(text):
        return True
    # 纯页码或“第 x 页”
    if re.fullmatch(r"(第\s*\d+\s*页|\d{1,4})", text):
        return True
    # 整行几乎只有点与空格（目录引导线被拆成 ". . . ." 的形态）
    symbol_ratio = sum(1 for ch in text if ch in ".．·… \t") / max(1, len(text))
    return symbol_ratio > 0.6


def clean_page_noise(blocks: list["TextBlock"], page_count: int) -> list["TextBlock"]:
    """去掉 PDF 页眉页脚与目录点线行。

    页眉页脚的特征是「同一行短文本在大量页面重复出现」，用词频统计识别，
    比写死字符串更通用；目录行按点线/页码特征过滤。
    """
    if page_count < HEADER_MIN_PAGES:
        return blocks

    counter: dict[str, set[int]] = {}
    for block in blocks:
        for line in block.text.splitlines():
            key = re.sub(r"\s+", "", line)
            if not key or len(key) > HEADER_MAX_CHARS:
                continue
            counter.setdefault(key, set()).add(block.page or 0)

    threshold = max(HEADER_MIN_PAGES, int(page_count * HEADER_MIN_PAGES_RATIO))
    repeated = {key for key, pages in counter.items() if len(pages) >= threshold}

    cleaned: list[TextBlock] = []
    for block in blocks:
        # 整块以目录行（点线 + 页码）为主时直接丢弃，避免目录混进正文切片
        lines = [line for line in block.text.splitlines() if line.strip()]
        if len(lines) >= 3:
            toc_lines = sum(1 for line in lines if _is_toc_line(line))
            if toc_lines / len(lines) > 0.5:
                continue
        kept_lines = []
        for line in block.text.splitlines():
            key = re.sub(r"\s+", "", line)
            if key in repeated or _is_toc_line(line):
                continue
            kept_lines.append(line)
        text = "\n".join(kept_lines).strip()
        if len(text) >= 10:
            cleaned.append(TextBlock(text=text, page=block.page))
    # 清理后过短说明过滤过度，退回原文
    if len(cleaned) < len(blocks) * 0.4:
        return blocks
    return cleaned


def parse_pdf(path: Path) -> ParsedDocument:
    import pymupdf

    blocks: list[TextBlock] = []
    try:
        with pymupdf.open(path) as document:
            for page_index, page in enumerate(document, start=1):
                # 用版式块而不是整页文本，保留段落边界，利于后续切片
                for raw_block in page.get_text("blocks"):
                    if len(raw_block) >= 7 and raw_block[6] != 0:
                        continue  # 跳过图片块
                    page_text = normalize_text(raw_block[4])
                    for paragraph in _split_paragraphs(page_text):
                        blocks.append(TextBlock(text=paragraph, page=page_index))
            page_count = document.page_count
    except Exception as exc:
        raise AppError("parse_failed", f"PDF 解析失败：{type(exc).__name__}", status_code=422) from exc

    if not blocks:
        raise AppError("empty_document", "PDF 中没有可提取的文本，可能是扫描件", status_code=422)

    blocks = clean_page_noise(blocks, page_count)
    return ParsedDocument(blocks=blocks, page_count=page_count, char_count=sum(len(b.text) for b in blocks))


def parse_docx(path: Path) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[TextBlock] = []
    try:
        document = Document(str(path))
        # 按文档真实顺序遍历段落与表格，避免段落被表格打断后顺序错乱
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = normalize_text(Paragraph(child, document).text)
                if text:
                    blocks.append(TextBlock(text=text, kind="paragraph"))
            elif tag == "tbl":
                rows: list[str] = []
                for row in Table(child, document).rows:
                    cells = [normalize_text(cell.text).replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    blocks.append(TextBlock(text="\n".join(rows), kind="table"))
    except AppError:
        raise
    except Exception as exc:
        raise AppError("parse_failed", f"DOCX 解析失败：{type(exc).__name__}", status_code=422) from exc

    if not blocks:
        raise AppError("empty_document", "DOCX 中没有可提取的文本", status_code=422)
    return ParsedDocument(blocks=blocks, page_count=0, char_count=sum(len(b.text) for b in blocks))


def parse_txt(path: Path) -> ParsedDocument:
    raw = path.read_bytes()
    if not raw.strip():
        raise AppError("empty_document", "文件内容为空", status_code=422)

    text: str | None = None
    used_encoding = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            used_encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise AppError("decode_failed", "文本编码无法识别，仅支持 UTF-8 与 GB18030", status_code=422)

    cleaned = normalize_text(text)
    blocks = [TextBlock(text=paragraph) for paragraph in _split_paragraphs(cleaned)]
    if not blocks:
        raise AppError("empty_document", f"文件内容为空（编码 {used_encoding}）", status_code=422)
    return ParsedDocument(blocks=blocks, page_count=0, char_count=sum(len(b.text) for b in blocks))


PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".txt": parse_txt,
}


def parse_document(path: Path | str, extension: str | None = None) -> ParsedDocument:
    """按扩展名选择解析器。"""
    path = Path(path)
    if not path.exists():
        raise AppError("file_not_found", "文件不存在", status_code=404)
    suffix = (extension or path.suffix).lower()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    parser = PARSERS.get(suffix)
    if parser is None:
        raise AppError("unsupported_format", f"暂不支持 {suffix} 格式", status_code=415)
    return parser(path)


def is_probably_scanned(parsed: ParsedDocument) -> bool:
    """粗略判断：页数正常但几乎无文本，多为扫描件。"""
    return parsed.page_count > 0 and parsed.char_count < 20 * max(parsed.page_count, 1)


def describe_encoding(path: Path) -> str:
    """用于调试输出：猜测文本文件编码。"""
    data = Path(path).read_bytes()[:4096]
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        try:
            data.decode("gb18030")
            return "gb18030"
        except UnicodeDecodeError:
            return "unknown"


def full_width_ratio(text: str) -> float:
    """全角字符占比，用于判断中文文档特征（测试与统计用）。"""
    if not text:
        return 0.0
    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in {"W", "F"})
    return wide / len(text)
