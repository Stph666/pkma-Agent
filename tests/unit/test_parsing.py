"""解析层测试：PDF 页码、DOCX 段落与表格顺序、TXT 编码回退、异常文件。"""

from __future__ import annotations

import pytest

from app.schemas import AppError
from app.services.parsing import (
    normalize_text,
    parse_document,
    parse_docx,
    parse_pdf,
    parse_txt,
)

pytest.importorskip("pymupdf")
pytest.importorskip("docx")


def _make_pdf(path, pages: int = 3):
    import pymupdf

    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {index + 1} content about retrieval augmented generation.")
        page.insert_text((72, 100), f"Second paragraph on page {index + 1}.")
    document.save(str(path))
    document.close()
    return path


def _make_docx(path):
    from docx import Document

    document = Document()
    document.add_heading("第一章 知识管理", level=1)
    document.add_paragraph("这是第一段正文，用于验证段落提取顺序。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "字段"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "切片"
    table.cell(1, 1).text = "500 字"
    document.add_paragraph("表格之后的段落，顺序应排在表格后面。")
    document.save(str(path))
    return path


def test_parse_pdf_keeps_page_numbers(tmp_path):
    parsed = parse_pdf(_make_pdf(tmp_path / "sample.pdf", pages=3))

    assert parsed.page_count == 3
    assert parsed.char_count > 0
    assert {block.page for block in parsed.blocks} == {1, 2, 3}
    assert any("Page 2" in block.text for block in parsed.blocks)


def test_parse_docx_keeps_document_order(tmp_path):
    parsed = parse_docx(_make_docx(tmp_path / "sample.docx"))
    texts = [block.text for block in parsed.blocks]

    assert any("第一章" in text for text in texts)
    table_block = next(block for block in parsed.blocks if block.kind == "table")
    assert "字段 | 说明" in table_block.text
    assert "切片 | 500 字" in table_block.text
    # 表格之后的段落必须排在表格块之后
    table_index = texts.index(table_block.text)
    assert any("表格之后" in text for text in texts[table_index + 1 :])


def test_parse_txt_utf8(tmp_path):
    path = tmp_path / "utf8.txt"
    path.write_text("第一段中文内容。\n\n第二段内容。", encoding="utf-8")

    parsed = parse_txt(path)
    assert [block.text for block in parsed.blocks] == ["第一段中文内容。", "第二段内容。"]


def test_parse_txt_gb18030_fallback(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("这是 GB18030 编码的文本。".encode("gb18030"))

    parsed = parse_txt(path)
    assert "GB18030" in parsed.blocks[0].text


def test_empty_txt_raises(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        parse_txt(path)
    assert excinfo.value.code == "empty_document"


def test_undecodable_txt_raises(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe\x00\x00\xff\xfe")

    with pytest.raises(AppError) as excinfo:
        parse_txt(path)
    assert excinfo.value.code in {"decode_failed", "empty_document"}


def test_unsupported_extension(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# 标题", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        parse_document(path)
    assert excinfo.value.code == "unsupported_format"


def test_missing_file(tmp_path):
    with pytest.raises(AppError) as excinfo:
        parse_document(tmp_path / "not-exists.pdf")
    assert excinfo.value.code == "file_not_found"


def test_normalize_text_strips_invisible_chars():
    raw = "标题\u200b\r\n\r\n\r\n\r\n正文\u00a0内容\t\t结尾   "
    cleaned = normalize_text(raw)

    assert "\u200b" not in cleaned
    assert "\u00a0" not in cleaned
    assert "\n\n\n" not in cleaned
    assert cleaned.startswith("标题")
    assert cleaned.endswith("结尾")
