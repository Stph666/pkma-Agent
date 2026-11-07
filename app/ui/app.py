"""Gradio 表现层：知识库、智能问答、知识图谱三个页面。"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

import gradio as gr
from sqlalchemy import func, select

from app.api.documents import list_documents
from app.api.health import collect_health
from app.config import settings
from app.db import session_scope
from app.models import Document, DocumentChunk
from app.schemas import AppError
from app.services import ingestion
from app.services.generation import SYSTEM_GENERAL

CSS_PATH = Path(__file__).with_name("styles.css")

STATUS_TEXT = {"processing": "处理中", "ready": "就绪", "failed": "失败"}
STATUS_CHOICES = ["全部", "就绪", "处理中", "失败"]
TABLE_HEADERS = ["文件名", "大小", "状态", "分类", "标签", "切片", "上传"]

TABLE_CSS = """
.doc-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.doc-table th { text-align: left; font-weight: 600; color: #1d1d1f; background: #f5f5f7;
  padding: 10px 12px; border-bottom: 1px solid #e8e8ed; white-space: nowrap; }
.doc-table td { padding: 10px 12px; border-bottom: 1px solid #f0f0f2; color: #3a3a3c; }
.doc-table tr:last-child td { border-bottom: none; }
.doc-table .name { color: #1d1d1f; font-weight: 500; }
.tag-pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2ff;
  color: #2563eb; font-size: 12px; margin-right: 4px; }
.state { font-size: 12px; padding: 2px 8px; border-radius: 999px; }
.state-ready { background: #e8f6ed; color: #1a7f37; }
.state-processing { background: #fff4e0; color: #a15c00; }
.state-failed { background: #fdeaea; color: #c0392b; }
"""



def _card(label: str, ok: bool, detail: str) -> str:
    dot = "ok" if ok else "bad"
    return (
        f'<div class="status-card"><div class="label">{label}</div>'
        f'<div class="value"><span class="dot {dot}"></span>{detail}</div></div>'
    )


def render_status() -> str:
    health = collect_health()
    cards = [
        _card("服务状态", True, f"v{health.version} 运行中"),
        _card("数据库", health.database.ok, health.database.detail),
        _card("向量存储", health.vector_store.ok, health.vector_store.detail),
        _card("生成模型", health.llm.ok, health.llm.detail),
    ]
    notice = ""
    if health.status != "ok":
        notice = (
            '<div style="margin-top:12px;font-size:13px;color:#a15c00;background:#fff8e6;'
            'border:1px solid #ffe0a3;border-radius:10px;padding:10px 14px;">'
            "降级运行：生成模型未配置，文档问答与摘要生成不可用；入库、检索与历史数据仍可正常查看。</div>"
        )
    return f'<div class="status-grid">{"".join(cards)}</div>{notice}'


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{max(1, size // 1024)} KB"


def _documents_html(items) -> str:
    """自绘文档表格：避免 Gradio 数据表在深色模式下的固定表头配色。"""
    if not items:
        return (
            '<div class="placeholder">还没有文档。上传 PDF / DOCX / TXT 后，'
            "这里会显示解析进度、分类与切片数。</div>"
        )

    head = "".join(f"<th>{escape(title)}</th>" for title in TABLE_HEADERS)
    body_rows = []
    for item in items:
        tag_list = list(item.tags or [])
        tags = "".join(f'<span class="tag-pill">{escape(tag)}</span>' for tag in tag_list[:3]) or "—"
        if len(tag_list) > 3:
            tags += f'<span class="tag-pill dim">+{len(tag_list) - 3}</span>'
        state = STATUS_TEXT.get(item.status, item.status)
        ext = item.extension.lstrip(".").lower() or "txt"
        badge = {"pdf": "PDF", "docx": "DOC", "txt": "TXT"}.get(ext, ext.upper()[:3])
        body_rows.append(
            "<tr>"
            f'<td class="name"><span class="ftype {ext}">{badge}</span>{escape(item.original_name)}</td>'
            f'<td class="num">{_format_size(item.size)}</td>'
            f'<td><span class="state state-{item.status}">{escape(state)}</span></td>'
            f'<td class="dim">{escape(item.category or "—")}</td>'
            f"<td>{tags}</td>"
            f'<td class="num">{item.chunk_count}</td>'
            f'<td class="dim">{item.created_at.strftime("%m-%d %H:%M")}</td>'
            "</tr>"
        )
    return (
        f"<style>{TABLE_CSS}</style>"
        f'<table class="doc-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def load_documents(status_filter: str = "全部") -> tuple[str, object, str]:
    """读取文档列表，同时返回下拉选项与统计文案。"""
    status = None if status_filter in (None, "", "全部") else {
        "就绪": "ready",
        "处理中": "processing",
        "失败": "failed",
    }.get(status_filter, status_filter)

    try:
        with session_scope() as session:
            items, counts = list_documents(session, status=status, limit=200)
    except Exception as exc:
        return (
            '<div class="placeholder">数据库不可用，暂时无法读取文档列表。</div>',
            gr.update(choices=[], value=None),
            f"⚠️ 数据库不可用（{type(exc).__name__}）：请确认 MySQL 已启动，并已执行 pkma 建库脚本。",
        )

    choices = [(f"{item.original_name}（{STATUS_TEXT.get(item.status, item.status)}）", item.id) for item in items]
    summary = (
        f"共 **{len(items)}** 份文档 ｜ 就绪 {counts.get('ready', 0)} · "
        f"处理中 {counts.get('processing', 0)} · 失败 {counts.get('failed', 0)}"
    )
    return _documents_html(items), gr.update(choices=choices, value=None), summary


def handle_upload(files, status_filter: str, overwrite: bool = False):
    if not files:
        return gr.update(), gr.update(), "请先选择 PDF、DOCX 或 TXT 文件。", None

    file_list = files if isinstance(files, list) else [files]
    messages: list[str] = []
    for item in file_list:
        path = Path(item if isinstance(item, str) else getattr(item, "name", str(item)))
        try:
            document = ingestion.register_upload(path, path.name, overwrite=overwrite)
            ingestion.start_ingest(document.id)
            messages.append(f"已接收《{document.original_name}》，正在解析与索引")
        except AppError as exc:
            messages.append(f"《{path.name}》未入库：{exc.message}")
        except Exception as exc:  # pragma: no cover - 兜底提示
            messages.append(f"《{path.name}》处理异常：{type(exc).__name__}")

    rows, dropdown, summary = load_documents(status_filter)
    return rows, dropdown, f"{summary}\n\n" + "\n\n".join(messages), None


def retry_failed_documents(status_filter: str):
    """批量重试失败文档：入库失败重跑解析，知识组织失败重跑抽取。"""
    from sqlalchemy import select as sa_select

    from app.services import enrichment

    messages: list[str] = []
    try:
        with session_scope() as session:
            failed = list(session.scalars(sa_select(Document).where(Document.status == "failed")).all())
            for document in failed:
                if not Path(document.stored_path).exists():
                    document.error_message = "原始文件已丢失，请重新上传"
                    messages.append(f"《{document.original_name}》原文件丢失，已跳过")
                    continue
                document.status = "processing"
                document.error_message = None
                session.commit()
                ingestion.start_ingest(document.id)
                messages.append(f"《{document.original_name}》已重新入库")

            pending = list(
                session.scalars(
                    sa_select(Document).where(
                        Document.status == "ready", Document.enrichment_status == "failed"
                    )
                ).all()
            )
            for document in pending:
                enrichment.start_enrich(document.id)
                messages.append(f"《{document.original_name}》已重新生成知识组织")
    except Exception as exc:
        messages.append(f"重试失败：{type(exc).__name__}")

    rows, dropdown, summary = load_documents(status_filter)
    detail = "\n\n".join(messages) if messages else "没有需要重试的文档。"
    return rows, dropdown, f"{summary}\n\n{detail}"


def handle_delete(document_id: str | None, status_filter: str):
    if not document_id:
        rows, dropdown, summary = load_documents(status_filter)
        return rows, dropdown, f"{summary}\n\n请先在下拉框选择文档。", ""
    try:
        ingestion.delete_document(document_id)
        message = "已删除文档及其向量、切片"
    except AppError as exc:
        message = exc.message
    rows, dropdown, summary = load_documents(status_filter)
    return rows, dropdown, f"{summary}\n\n{message}", ""


def handle_reindex(document_id: str | None, status_filter: str):
    if not document_id:
        rows, dropdown, summary = load_documents(status_filter)
        return rows, dropdown, f"{summary}\n\n请先在下拉框选择文档。", ""
    try:
        ingestion.reindex_document(document_id)
        message = "已开始重建索引，请稍后刷新查看状态"
    except AppError as exc:
        message = exc.message
    rows, dropdown, summary = load_documents(status_filter)
    return rows, dropdown, f"{summary}\n\n{message}", ""


def show_detail(document_id: str | None) -> str:
    if not document_id:
        return '<div class="placeholder">在下拉框选择文档后查看详情。</div>'

    try:
        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                return '<div class="placeholder">文档不存在或已删除。</div>'
            chunks = session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
                .limit(3)
            ).all()
    except Exception as exc:
        return f'<div class="placeholder">读取详情失败：{type(exc).__name__}</div>'

    tags = "、".join(document.tags_json or []) or "—"
    preview = "".join(
        f'<div style="margin-top:10px;padding:12px 14px;background:#f5f5f7;border-radius:10px;'
        f'font-size:13px;color:#3a3a3c;line-height:1.7;">'
        f'<div style="color:#86868b;font-size:12px;margin-bottom:6px;">切片 #{row.chunk_index}'
        f'{" ｜ 第 " + str(row.page_start) + " 页" if row.page_start else ""}</div>'
        f"{row.content[:260]}{'…' if len(row.content) > 260 else ''}</div>"
        for row in chunks
    )

    enrich_state = document.enrichment_status or "pending"
    state_text = {
        "pending": "尚未生成",
        "processing": "生成中…",
        "done": "已生成",
        "failed": "生成失败",
    }.get(enrich_state, enrich_state)
    if enrich_state == "done":
        summary_block = escape((document.summary or "（摘要为空）")[:500])
    elif enrich_state == "failed":
        summary_block = f"知识组织失败：{escape((document.enrichment_error or '未知原因')[:200])}"
    else:
        summary_block = "点击下方「生成摘要与图谱」按钮，由模型生成摘要、分类、标签与实体关系。"

    return (
        '<div class="status-card" style="margin-top:8px;">'
        f'<div class="label">{escape(document.original_name)}</div>'
        f'<div class="value">状态：{STATUS_TEXT.get(document.status, document.status)}　'
        f"分类：{escape(document.category or '—')}　标签：{escape(tags)}　"
        f"知识组织：{state_text}</div>"
        f'<div style="margin-top:10px;font-size:13px;color:#3a3a3c;line-height:1.7;">{summary_block}</div>'
        f"{preview}</div>"
    )


def _conversations_dropdown(selected: str | None = None):
    from app.services import conversations as conversation_service

    try:
        with session_scope() as session:
            items = conversation_service.list_conversations(session, limit=50)
        choices = [(item.title or "未命名", item.id) for item in items]
    except Exception:
        choices = []
    return gr.update(choices=choices, value=selected)


def new_conversation() -> tuple[str, list, str, object, str]:
    from app.services import conversations as conversation_service

    with session_scope() as session:
        conversation = conversation_service.create_conversation(session)
        conversation_id = conversation.id
    return (
        conversation_id,
        [],
        '<p class="sources-note">新对话已创建，回答后这里会列出引用来源。</p>',
        _conversations_dropdown(conversation_id),
        "",
        gr.update(value=_empty_state_html(), visible=True),
    )


def load_conversation(conversation_id: str | None):
    if not conversation_id:
        return [], gr.update(), "", None, gr.update(value=_empty_state_html(), visible=True)
    from app.services import conversations as conversation_service

    try:
        with session_scope() as session:
            messages = conversation_service.list_messages(session, conversation_id)
    except Exception:
        return [], '<div class="placeholder">会话不存在。</div>', "", None

    history: list[dict] = []
    citations: list[dict] = []
    for message in messages:
        content = message.content or ("（生成失败）" if message.status == "error" else "")
        history.append(
            {
                "role": message.role,
                "content": decorate_answer(content) if message.role == "assistant" else content,
            }
        )
        if message.role == "assistant" and message.citations_json:
            citations = list(message.citations_json)
    empty = gr.update(visible=False) if messages else gr.update(value=_empty_state_html(), visible=True)
    return history, render_citations(citations), f"历史消息 {len(messages)} 条", conversation_id, empty


def render_citations(citations: list[dict]) -> str:
    """来源卡：序号 + 文件名 + 页码 + 3 行摘要 + 相似度 + 查看原文。"""
    if not citations:
        return (
            '<div class="sources-head"><span class="sources-title">来源</span>'
            '<span class="count">0 条</span></div>'
            '<p class="sources-note">回答后这里会按相似度列出命中的原文片段，'
            "角标与来源一一对应，点击可互相定位。</p>"
        )
    cards = []
    for item in citations:
        index = item.get("index")
        if item.get("origin") == "graph":
            origin = "图谱补充"
        else:
            score = item.get("score") or 0
            origin = f"相似度 {float(score):.2f}"
        cards.append(
            f'<article class="src" data-src="{index}">'
            '<div class="src-top">'
            f'<span class="src-idx">{index}</span>'
            f'<span class="src-name">{escape(str(item.get("filename", "")))}</span>'
            f'<span class="src-page">{escape(str(item.get("location", "")))}</span>'
            "</div>"
            f'<p class="src-text">{escape(str(item.get("snippet", "")))}</p>'
            + (
                f'<p class="src-quote">“{escape(str(item.get("quote")))}”</p>'
                if item.get("quote")
                else ""
            )
            + f'<div class="src-full" hidden>{escape(str(item.get("content") or item.get("snippet", "")))}</div>'
            f'<div class="src-foot"><span class="src-score">{escape(origin)}</span>'
            '<a class="src-open" role="button" tabindex="0">查看原文 →</a></div>'
            "</article>"
        )
    head = (
        '<div class="sources-head"><span class="sources-title">来源</span>'
        f'<span class="count">{len(citations)} 条 · 按相似度排序</span></div>'
    )
    return head + '<div class="src-list">' + "".join(cards) + "</div>"


def decorate_answer(text: str) -> str:
    """把答案里的 [1] 变成可点击角标，点击可定位到来源卡。"""
    if not text:
        return text

    def _replace(match: re.Match) -> str:
        index = match.group(1)
        return f'<span class="cite" data-cite="{index}">{index}</span>'

    return re.sub(r"\[(\d{1,2})\]", _replace, text)


def send_message(message: str, history: list, mode_label: str, doc_ids: list, conversation_id: str | None):
    """流式问答：逐段产出对话与引用。"""
    from app.services import qa

    text = (message or "").strip()
    if not text:
        yield (
            history, gr.update(), "请输入问题。", conversation_id, "",
            gr.update(visible=False), gr.update(visible=False),
        )
        return

    mode = "general" if mode_label == "通用对话" else "knowledge"
    if isinstance(doc_ids, str):
        doc_ids = [doc_ids] if doc_ids else []
    history = list(history or []) + [{"role": "user", "content": text}, {"role": "assistant", "content": ""}]
    answer = ""
    citations: list[dict] = []
    stop_button_visible = True
    status = "检索中…"
    yield (
        history, gr.update(), status, conversation_id, "",
        gr.update(visible=False), gr.update(visible=True),
    )

    events = qa.stream_events(
        text,
        conversation_id=conversation_id or None,
        mode=mode,
        document_ids=list(doc_ids or []),
    )
    status_label = "知识库" if mode == "knowledge" else "通用对话"
    try:
        for event, payload in events:
            if event == "meta":
                conversation_id = payload.get("conversation_id", conversation_id)
                if mode == "knowledge":
                    retrieved = payload.get("retrieved", 0)
                    status = f"检索到 {retrieved} 条依据，正在生成…" if retrieved else "正在生成…"
            elif event == "delta":
                answer += payload.get("text", "")
                history[-1]["content"] = decorate_answer(answer)
            elif event == "citations":
                citations = payload.get("citations", [])
            elif event == "error":
                history[-1]["content"] = payload.get("message", "生成失败")
                status = f"生成失败：{payload.get('message', '')}"
            elif event == "done":
                stop_button_visible = False
                if payload.get("status") == "no_evidence":
                    status = "知识库中没有足够依据，可点「改用通用知识重答」"
                elif payload.get("status") == "done":
                    cited = payload.get("cited") or []
                    status = (
                        f"完成 · 耗时 {payload.get('elapsed')}s · 引用 {', '.join(f'[{i}]' for i in cited) or '未标注'}"
                    )
            yield (
                history,
                render_citations(citations),
                f"[{status_label}] {status}",
                conversation_id,
                "",
                gr.update(visible=False),
                gr.update(visible=bool(stop_button_visible)),
            )
    finally:
        events.close()


def regenerate(history: list, mode_label: str, doc_ids: list, conversation_id: str | None):
    """替换最后一条回答：先删库里的旧回答，再去掉界面上的旧气泡重新生成。"""
    from app.services import conversations as conversation_service

    items = list(history or [])
    last_user = next((item.get("content", "") for item in reversed(items) if item.get("role") == "user"), "")
    if not last_user.strip():
        yield items, gr.update(), "还没有可重新生成的问题。", conversation_id, "", gr.update(), gr.update(visible=False)
        return

    # 去掉末尾的助手消息（可能多条），保留到最后一个 user 为止
    while items and items[-1].get("role") == "assistant":
        items.pop()

    if conversation_id:
        try:
            with session_scope() as session:
                conversation_service.delete_last_assistant(session, conversation_id)
        except Exception:
            pass

    yield from send_message(last_user, items, mode_label, doc_ids, conversation_id)


def retry_general(history: list, doc_ids: list, conversation_id: str | None):
    """用上一条问题切到通用模式重答。"""
    last_user = next(
        (item.get("content", "") for item in reversed(history or []) if item.get("role") == "user"),
        "",
    )
    if not last_user.strip():
        yield history, gr.update(), "还没有可重答的问题。", conversation_id, "", gr.update(), gr.update(visible=False)
        return
    yield from send_message(last_user, history, "通用对话", doc_ids, conversation_id)


DEPTH_CHOICES = [("快速（1%，约 5 次调用）", "fast"), ("标准（2%，推荐）", "standard"), ("深度（5%，较慢）", "deep")]


def organize_document(document_id: str | None, depth_label: str | None = None):
    """按所选档位触发摘要/分类/标签/图谱生成。"""
    from app.schemas import AppError
    from app.services import enrichment

    if not document_id:
        return show_detail(document_id), "请先在下拉框选择文档。"

    depth = "standard"
    for label, key in DEPTH_CHOICES:
        if depth_label and depth_label.startswith(label[:2]):
            depth = key
            break

    try:
        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise AppError("document_not_found", "文档不存在", status_code=404)
            if document.status != "ready":
                raise AppError("document_not_ready", "文档尚未完成入库，暂时无法生成知识组织", status_code=409)
            if not settings.llm_configured:
                raise AppError("llm_not_configured", "未配置生成模型，无法生成摘要与图谱", status_code=503)
            total_chunks = session.scalar(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
            ) or 0
            document.enrichment_status = "processing"
            document.enrichment_error = None

        plan = enrichment.estimate_cost(int(total_chunks), depth)
        enrichment.start_enrich(document_id, depth=depth)
        return show_detail(document_id), (
            f"已按「{plan['label']}」档开始生成：抽样 {plan['chunks']} 片、约 {plan['calls']} 次调用、"
            f"预计 {plan['seconds']} 秒。完成后再点「查看详情」刷新。"
        )
    except AppError as exc:
        return show_detail(document_id), exc.message



def ask_about_entity(entity: str | None, document_id: str | None):
    """把实体问题预填到问答页，并把文档范围限定到当前文档。"""
    name = (entity or "").strip()
    if not name:
        return gr.update(), gr.update(), "请先填写实体名（点击图上节点后把名称填进来）。"
    question = f"「{name}」是什么？它在文档中起什么作用？"
    scope = document_id or ""
    return (
        question,
        gr.update(value=scope),
        f"已填入问题并限定范围：{name}。请切到「智能问答」页点发送。",
    )


def _load_chat_sidebars():
    """初始化问答页的文档范围与会话列表。"""
    from app.services import conversations as conversation_service

    try:
        with session_scope() as session:
            items, _ = list_documents(session, status="ready", limit=200)
        doc_choices = [(item.original_name, item.id) for item in items]
    except Exception:
        doc_choices = []
    return gr.update(choices=[("全部文档", "")] + doc_choices, value=""), _conversations_dropdown()



def _graph_document_choices():
    try:
        with session_scope() as session:
            items, _ = list_documents(session, status="ready", limit=200)
        return [("全部文档", "")] + [(item.original_name, item.id) for item in items]
    except Exception:
        return [("全部文档", "")]


# 图谱页只保留一个「显示范围」控件：内部映射到 limit / include_isolated。
# 不单独暴露「最小关联度」和「包含孤立实体」——三个控件管同一件事，用户无法判断谁在起作用。
GRAPH_RANGE_CHOICES = [
    ("自动（≤40 个全显示）", 0),
    ("只看主要实体（关联度前 40）", 40),
    ("全部实体（含孤立）", -1),
]
ENTITY_TYPES = ["技术", "概念", "工具", "指标", "组织", "其他"]


def load_graph(
    document_id: str | None,
    query: str,
    entity_range: int | str | None = 0,
    types: list[str] | None = None,
):
    """读取图谱并生成可视化，返回 (图, 统计行, 说明行)。"""
    from app.api.graph import get_graph
    from app.ui.graph_view import build_graph_html

    try:
        limit = int(entity_range)
    except (TypeError, ValueError):
        limit = 0
    include_isolated = limit < 0
    api_limit = None if limit < 0 else limit  # 0 交给接口自适应：≤40 全显示，否则取 40

    try:
        with session_scope() as session:
            payload = get_graph(
                document_id=document_id or None,
                q=query or None,
                types=list(types) if types else None,
                min_degree=0,
                include_isolated=include_isolated,
                limit=api_limit,
                session=session,
            )
    except Exception as exc:
        return (
            f'<div class="placeholder">图谱读取失败：{type(exc).__name__}</div>',
            "图谱不可用",
            "",
        )

    if not payload.nodes:
        return (
            '<div class="placeholder">暂无图谱数据。先上传文档并生成知识组织，这里会显示实体与关系。</div>',
            "当前筛选下没有实体",
            "",
        )

    html = build_graph_html(
        [node.model_dump() for node in payload.nodes],
        [edge.model_dump() for edge in payload.edges],
    )

    stats = (
        f"已显示 **{payload.shown_entities} / {payload.total_entities}** 个实体"
        "（按关联度从高到低）"
    )
    if payload.truncated:
        stats += f"｜其余 {payload.total_entities - payload.shown_entities} 个已折叠"

    hist = payload.degree_histogram or {}
    degree_note = (
        f"关联度＝该实体连出的关系条数：≥3 的 {hist.get('high', 0)} 个、"
        f"1–2 的 {hist.get('low', 0)} 个、孤立（0 条）{hist.get('isolated', 0)} 个。"
        "多数实体只出现在一个片段里，所以低关联和孤立的最多，这是正常的长尾分布。"
    )

    relations_text = (
        f"关系 {payload.shown_relations} 条"
        if payload.shown_relations >= payload.total_relations
        else f"关系 {payload.shown_relations} / {payload.total_relations} 条（两端都在可见范围内才画）"
    )
    if payload.sampled_chunks:
        ratio = payload.sampled_chunks / payload.total_chunks * 100 if payload.total_chunks else 0
        scope_note = (
            f"基于 {payload.sampled_chunks} / {payload.total_chunks} 个切片抽取"
            f"（{payload.depth_label}档 · {ratio:.0f}%）｜{relations_text}"
        )
    else:
        scope_note = f"检索范围共 {payload.total_chunks} 个切片｜{relations_text}"

    names = [node.name for node in payload.nodes]
    return html, stats, f"{degree_note}\n\n{scope_note}", gr.update(choices=names)



def _brand_html() -> str:
    return (
        '<div class="brand"><span class="brand-mark"></span>'
        '<span class="brand-name">个人知识库</span></div>'
    )


def _rail_status_html() -> str:
    health = collect_health()
    db_dot = "ok" if health.database.ok else "bad"
    llm_dot = "ok" if health.llm.ok else "bad"
    model = "已连接" if health.llm.ok else "未配置"
    return (
        '<div class="rail-foot">'
        f'<div class="row"><span class="dot {db_dot}"></span>数据库 {"正常" if health.database.ok else "异常"}</div>'
        f'<div class="row"><span class="dot {llm_dot}"></span>模型 {model} · {escape(settings.llm_model or "-")}</div>'
        "</div>"
    )


def _chat_head_html(title: str = "新会话") -> str:
    return (
        '<div class="chat-head">'
        f'<span class="title">{escape(title)}</span>'
        '<span class="tag">知识库问答</span>'
        "</div>"
    )


def _empty_state_html() -> str:
    samples = [
        "增量更新时为什么要先删旧向量？",
        "切片长度对召回有什么影响？",
        "知识图谱里的三元组是什么？",
    ]
    chips = "".join(
        f'<button class="chip" data-sample="{escape(text)}">{escape(text)}</button>' for text in samples
    )
    return (
        '<div class="empty-card">'
        '<div class="empty-icon">✦</div>'
        '<h2>从你的资料里找答案</h2>'
        '<p>提问后会先检索知识库，再给出带引用的回答；没有依据时会明确告诉你。</p>'
        f'<div class="empty-chips">{chips}</div>'
        "</div>"
    )


def _sources_head_html(count: int, top_score: float | None = None) -> str:
    extra = f"最高相似度 {top_score:.2f}" if top_score else "按相似度排序"
    return (
        '<div class="sources-head"><span class="sources-title">来源</span>'
        f'<span class="count">{count} 条 · {extra}</span></div>'
    )



UI_JS = """
() => {
  const bind = () => {
    // 角标 -> 来源卡
    document.querySelectorAll('.cite:not([data-bound])').forEach((el) => {
      el.dataset.bound = '1';
      el.addEventListener('click', (event) => {
        event.stopPropagation();
        const idx = el.dataset.cite;
        document.querySelectorAll('.cite').forEach((c) => c.classList.toggle('lit', c.dataset.cite === idx));
        document.querySelectorAll('.src').forEach((s) => s.classList.toggle('lit', s.dataset.src === idx));
        const card = document.querySelector('.src[data-src="' + idx + '"]');
        if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    });

    // 来源卡 -> 角标
    document.querySelectorAll('.src:not([data-bound])').forEach((el) => {
      el.dataset.bound = '1';
      el.addEventListener('click', () => {
        const idx = el.dataset.src;
        document.querySelectorAll('.cite').forEach((c) => c.classList.toggle('lit', c.dataset.cite === idx));
        document.querySelectorAll('.src').forEach((s) => s.classList.toggle('lit', s === el));
      });
    });

    // 查看原文 -> 右侧阅读面板（在同一列内翻阅）
    const openReader = (anchor) => {
      const card = anchor.closest('.src');
      if (!card) return;
      const cards = [...document.querySelectorAll('.src')];
      const index = cards.indexOf(card);
      const panel = document.querySelector('.sources-col');
      if (!panel) return;

      let reader = panel.querySelector('.reader');
      if (!reader) {
        reader = document.createElement('div');
        reader.className = 'reader';
        panel.appendChild(reader);
      }
      const name = card.querySelector('.src-name').innerText;
      const page = card.querySelector('.src-page').innerText;
      const full = card.querySelector('.src-full');
      const text = full ? full.innerText : card.querySelector('.src-text').innerText;

      reader.innerHTML =
        '<div class="reader-head"><button class="reader-back">← 来源</button>' +
        '<span class="reader-title">' + name + '</span>' +
        '<span class="reader-page">' + page + '</span></div>' +
        '<div class="reader-body">' + text.replace(/</g, '&lt;') + '</div>' +
        '<div class="reader-foot"><button class="reader-prev"' + (index <= 0 ? ' disabled' : '') +
        '>上一条</button><span class="reader-count">' + (index + 1) + ' / ' + cards.length +
        '</span><button class="reader-next"' + (index >= cards.length - 1 ? ' disabled' : '') +
        '>下一条</button></div>';

      panel.classList.add('reading');
      reader.querySelector('.reader-back').onclick = () => panel.classList.remove('reading');
      const prev = reader.querySelector('.reader-prev');
      const next = reader.querySelector('.reader-next');
      if (prev && !prev.disabled) prev.onclick = () => openReader(cards[index - 1].querySelector('.src-open'));
      if (next && !next.disabled) next.onclick = () => openReader(cards[index + 1].querySelector('.src-open'));
    };

    document.querySelectorAll('.src-open:not([data-bound])').forEach((el) => {
      el.dataset.bound = '1';
      el.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        openReader(el);
      });
    });

    // 示例问题 -> 输入框
    document.querySelectorAll('.chip[data-sample]:not([data-bound])').forEach((el) => {
      el.dataset.bound = '1';
      el.addEventListener('click', () => {
        const box = document.querySelector('.composer-row textarea');
        if (!box) return;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(box, el.dataset.sample);
        box.dispatchEvent(new Event('input', { bubbles: true }));
        box.focus();
      });
    });
  };

  bind();
  new MutationObserver(bind).observe(document.body, { childList: true, subtree: true });
}
"""




def save_general_prompt(text: str):
    """保存通用对话的系统提示词（运行期生效）。"""
    from app.services import generation as generation_service

    value = generation_service.set_general_prompt(text)
    return f"已保存，{len(value)} 字，下一轮通用对话生效。"



def dedupe_conversations():
    """把标题相同的会话只保留最新一条，清掉反复测试留下的重复项。"""
    from sqlalchemy import select as sa_select

    from app.models import Conversation

    removed = 0
    try:
        with session_scope() as session:
            items = list(
                session.scalars(sa_select(Conversation).order_by(Conversation.updated_at.desc())).all()
            )
            seen: set[str] = set()
            for item in items:
                title = (item.title or "").strip()
                if title and title in seen:
                    session.delete(item)
                    removed += 1
                else:
                    seen.add(title)
        message = f"已清理 {removed} 条重复会话" if removed else "没有重复会话"
    except Exception as exc:
        message = f"清理失败：{type(exc).__name__}"
    return _conversations_dropdown(), message


def delete_conversation(conversation_id: str | None):
    """删除当前会话并把界面恢复到空状态。"""
    from app.services import conversations as conversation_service

    if not conversation_id:
        return _conversations_dropdown(), [], render_citations([]), None, "请先在左侧选择会话。"

    try:
        with session_scope() as session:
            conversation_service.delete_conversation(session, conversation_id)
        message = "已删除会话"
    except Exception as exc:
        message = f"删除失败：{type(exc).__name__}"

    return _conversations_dropdown(), [], render_citations([]), None, message



def build_interface() -> gr.Blocks:
    with gr.Blocks(title=settings.app_name, analytics_enabled=False, fill_width=True) as demo:
        conversation_state = gr.State(None)
        with gr.Row(elem_classes="app-shell"):
            # ================= 左栏 =================
            with gr.Column(elem_classes="rail", scale=0, min_width=248):
                gr.HTML(_brand_html())
                new_chat_button = gr.Button("＋ 新建会话", elem_classes="btn-new")
                nav_radio = gr.Radio(
                    choices=["智能问答", "知识库", "知识图谱"],
                    value="智能问答",
                    show_label=False,
                    container=False,
                    elem_classes="nav-radio",
                )
                gr.HTML('<div class="rail-divider"></div><div class="rail-label">历史会话</div>')
                conversation_dd = gr.Radio(
                    choices=[],
                    value=None,
                    show_label=False,
                    container=False,
                    elem_classes="conv-radio",
                )
                delete_chat_button = gr.Button("删除当前会话", size="sm", elem_classes="rail-delete")
                dedupe_chat_button = gr.Button("清理重复会话", size="sm", elem_classes="rail-delete ghost")
                rail_status = gr.HTML(_rail_status_html())

            # ================= 主区 =================
            with gr.Column(elem_classes="main-col", scale=1):
                # ---------- 问答 ----------
                with gr.Column(visible=True, elem_classes="view view-chat") as chat_view:
                    with gr.Row(elem_classes="chat-grid"):
                        with gr.Column(elem_classes="chat-col", scale=1):
                            chat_head = gr.HTML(_chat_head_html())
                            empty_state = gr.HTML(_empty_state_html(), elem_classes="empty-wrap")
                            chatbot = gr.Chatbot(
                                show_label=False,
                                container=False,
                                placeholder="",
                                buttons=["copy"],
                                render_markdown=True,
                                sanitize_html=False,
                                height=520,
                                elem_classes="thread",
                            )
                            with gr.Row(elem_classes="ai-actions"):
                                regenerate_button = gr.Button("↻ 重新生成", size="sm", elem_classes="link-btn")
                                stop_button = gr.Button(
                                    "停止生成", size="sm", elem_classes="link-btn danger", visible=False
                                )

                            with gr.Accordion("通用对话系统提示词", open=False, elem_classes="prompt-acc"):
                                general_prompt_box = gr.Textbox(
                                    value=SYSTEM_GENERAL,
                                    lines=5,
                                    show_label=False,
                                    elem_classes="prompt-box",
                                )
                                with gr.Row(elem_classes="prompt-actions"):
                                    save_prompt_button = gr.Button(
                                        "保存", size="sm", elem_classes="btn-secondary"
                                    )
                                    prompt_status = gr.Markdown("", elem_classes="hint")

                            with gr.Row(elem_classes="composer-row"):
                                message_box = gr.Textbox(
                                    placeholder="提出你的问题，答案会标注引用来源",
                                    show_label=False,
                                    container=False,
                                    lines=2,
                                    max_lines=6,
                                    elem_classes="composer-input",
                                )
                                send_button = gr.Button("发送 ↵", elem_classes="btn-primary send-btn")
                            with gr.Row(elem_classes="composer-bar"):
                                mode_radio = gr.Radio(
                                    ["知识库", "通用对话"],
                                    value="知识库",
                                    show_label=False,
                                    container=False,
                                    elem_classes="chip-radio",
                                )
                                scope_dd = gr.Dropdown(
                                    choices=[("全部文档", "")],
                                    value="",
                                    label="文档范围",
                                    show_label=True,
                                    container=False,
                                    elem_classes="chip-select",
                                )
                                status_md = gr.Markdown("", elem_classes="status-line")

                        with gr.Column(elem_classes="sources-col", scale=0, min_width=336):
                            citations_html = gr.HTML(render_citations([]))

                # ---------- 知识库 ----------
                with gr.Column(visible=False, elem_classes="view view-kb") as kb_view:
                    gr.HTML(
                        '<div class="page-head"><div><h1 class="page-title">知识库</h1>'
                        '<p class="page-sub">支持 PDF / DOCX / TXT，单文件不超过 20 MB；'
                        "上传后自动解析、切片并建立向量索引。</p></div></div>"
                    )
                    with gr.Row(elem_classes="dropzone"):
                        with gr.Column(scale=3, elem_classes="dropzone-main"):
                            upload_box = gr.File(
                                label="拖到这里，或点击选择文件",
                                file_count="multiple",
                                file_types=list(settings.allowed_extension_set),
                                type="filepath",
                                height=96,
                            )
                        with gr.Column(scale=1, min_width=180, elem_classes="dropzone-side"):
                            upload_button = gr.Button("上传并入库", elem_classes="btn-primary")
                            overwrite_box = gr.Checkbox(value=False, label="覆盖同名文档")
                            gr.Markdown(
                                f"单份建议不超过 {settings.max_document_chars // 10000} 万字"
                                f"（约 {settings.max_document_pages_warn} 页）；超出会先提示，可强制继续。",
                                elem_classes="hint",
                            )

                    status_message = gr.Markdown("尚无文档。上传后这里会显示处理进度。", elem_classes="hint")

                    with gr.Row(elem_classes="toolbar"):
                        status_filter = gr.Radio(
                            STATUS_CHOICES,
                            value="全部",
                            show_label=False,
                            container=False,
                            elem_classes="filter-radio",
                        )
                        refresh_button = gr.Button("刷新列表", elem_classes="btn-secondary", size="sm")
                        retry_button = gr.Button("重试失败文档", elem_classes="btn-secondary", size="sm")

                    with gr.Row(elem_classes="kb-grid"):
                        with gr.Column(scale=1, min_width=0, elem_classes="kb-table-col"):
                            document_table = gr.HTML(
                                '<div class="skeleton-table">'
                                + "".join('<div class="skeleton-row"></div>' for _ in range(4))
                                + "</div>",
                                elem_classes="table-card",
                            )
                        with gr.Column(scale=0, min_width=320, elem_classes="kb-detail-col"):
                            with gr.Column(elem_classes="detail-card"):
                                document_picker = gr.Dropdown(
                                    choices=[], label="选择文档", elem_classes="detail-select"
                                )
                                document_detail = gr.HTML(
                                    '<p class="sources-note">选择文档后查看摘要、标签与切片。</p>'
                                )
                                with gr.Row(elem_classes="detail-actions"):
                                    detail_button = gr.Button("查看详情", size="sm", elem_classes="btn-secondary")
                                    organize_depth = gr.Dropdown(
                                        choices=[label for label, _ in DEPTH_CHOICES],
                                        value=DEPTH_CHOICES[1][0],
                                        show_label=False,
                                        container=False,
                                        elem_classes="detail-select",
                                    )
                                with gr.Row(elem_classes="detail-actions"):
                                    organize_button = gr.Button(
                                        "生成摘要与图谱", size="sm", elem_classes="btn-secondary"
                                    )
                                    reindex_button = gr.Button("重新索引", size="sm", elem_classes="btn-secondary")
                                    delete_button = gr.Button("删除", size="sm", elem_classes="link-btn danger")

                # ---------- 知识图谱 ----------
                with gr.Column(visible=False, elem_classes="view view-graph") as graph_view:
                    gr.HTML(
                        '<div class="page-head"><div><h1 class="page-title">知识图谱</h1>'
                        '<p class="page-sub">实体与关系来自文档抽取，按关联度排序；'
                        "点击节点或连线可查看来源片段。</p></div></div>"
                    )
                    with gr.Row(elem_classes="toolbar"):
                        graph_doc = gr.Dropdown(
                            choices=[("全部文档", "")], value="", show_label=False, elem_classes="tb-select"
                        )
                        graph_query = gr.Textbox(
                            placeholder="搜索实体", show_label=False, container=False, elem_classes="tb-input"
                        )
                        graph_limit = gr.Dropdown(
                            choices=GRAPH_RANGE_CHOICES, value=0, show_label=False, elem_classes="tb-select"
                        )
                        graph_types = gr.Dropdown(
                            choices=ENTITY_TYPES,
                            multiselect=True,
                            label="实体类型",
                            elem_classes="tb-select",
                        )
                        graph_button = gr.Button("刷新", size="sm", elem_classes="btn-secondary")
                    with gr.Row(elem_classes="toolbar"):
                        entity_question = gr.Dropdown(
                            choices=[],
                            allow_custom_value=True,
                            label="实体名",
                            elem_classes="tb-input",
                        )
                        ask_entity_button = gr.Button("就此实体提问", size="sm", elem_classes="btn-secondary")
                        graph_detail_button = gr.Button("查看该文档详情", size="sm", elem_classes="btn-secondary")

                    graph_detail_html = gr.HTML("")
                    graph_status = gr.Markdown("", elem_classes="status-line")
                    graph_status2 = gr.Markdown("", elem_classes="hint")
                    graph_html = gr.HTML('<div class="skeleton-block"></div>', elem_classes="graph-card")

                with gr.Accordion("系统状态", open=False, elem_classes="sys-accordion"):
                    status_html = gr.HTML(render_status())
                    refresh = gr.Button("刷新状态", variant="secondary", size="sm")
                    refresh.click(fn=render_status, outputs=status_html, queue=False, show_progress="hidden")

        # ================= 事件绑定 =================
        nav_radio.change(
            fn=switch_view,
            inputs=[nav_radio],
            outputs=[chat_view, kb_view, graph_view],
            queue=False,
        )
        new_chat_button.click(
            fn=new_conversation,
            outputs=[conversation_state, chatbot, citations_html, conversation_dd, status_md, empty_state],
        )
        dedupe_chat_button.click(
            fn=dedupe_conversations,
            outputs=[conversation_dd, status_md],
            queue=False,
        )
        delete_chat_button.click(
            fn=delete_conversation,
            inputs=[conversation_state],
            outputs=[conversation_dd, chatbot, citations_html, conversation_state, status_md],
        )
        conversation_dd.change(
            fn=load_conversation,
            inputs=[conversation_dd],
            outputs=[chatbot, citations_html, status_md, conversation_state, empty_state],
            queue=False,
        )
        conversation_dd.change(
            fn=lambda cid: _chat_head_html(dict(_conversation_titles()).get(cid, "新会话")),
            inputs=[conversation_dd],
            outputs=[chat_head],
            queue=False,
        )

        send_outputs = [
            chatbot,
            citations_html,
            status_md,
            conversation_state,
            message_box,
            empty_state,
            stop_button,
        ]
        send_inputs = [message_box, chatbot, mode_radio, scope_dd, conversation_state]
        send_event = send_button.click(fn=send_message, inputs=send_inputs, outputs=send_outputs)
        message_box.submit(fn=send_message, inputs=send_inputs, outputs=send_outputs)
        stop_button.click(fn=None, cancels=[send_event], queue=False)
        regenerate_button.click(
            fn=regenerate,
            inputs=[chatbot, mode_radio, scope_dd, conversation_state],
            outputs=send_outputs,
        )
        save_prompt_button.click(
            fn=save_general_prompt,
            inputs=[general_prompt_box],
            outputs=[prompt_status],
            queue=False,
        )
        upload_button.click(
            fn=handle_upload,
            inputs=[upload_box, status_filter, overwrite_box],
            outputs=[document_table, document_picker, status_message, upload_box],
        )
        refresh_button.click(
            fn=load_documents,
            inputs=[status_filter],
            outputs=[document_table, document_picker, status_message],
            queue=False,
            show_progress="hidden",
        )
        status_filter.change(
            fn=load_documents,
            inputs=[status_filter],
            outputs=[document_table, document_picker, status_message],
            queue=False,
            show_progress="hidden",
        )
        retry_button.click(
            fn=retry_failed_documents,
            inputs=[status_filter],
            outputs=[document_table, document_picker, status_message],
        )
        detail_button.click(
            fn=show_detail,
            inputs=[document_picker],
            outputs=[document_detail],
            queue=False,
            show_progress="hidden",
        )
        organize_button.click(
            fn=organize_document,
            inputs=[document_picker, organize_depth],
            outputs=[document_detail, status_message],
        )
        reindex_button.click(
            fn=handle_reindex,
            inputs=[document_picker, status_filter],
            outputs=[document_table, document_picker, status_message, document_detail],
        )
        delete_button.click(
            fn=handle_delete,
            inputs=[document_picker, status_filter],
            outputs=[document_table, document_picker, status_message, document_detail],
        )

        graph_inputs = [graph_doc, graph_query, graph_limit, graph_types]
        graph_outputs = [graph_html, graph_status, graph_status2, entity_question]
        for trigger in (
            graph_button.click,
            graph_doc.change,
            graph_limit.change,
            graph_types.change,
        ):
            trigger(fn=load_graph, inputs=graph_inputs, outputs=graph_outputs, queue=False)
        graph_query.submit(fn=load_graph, inputs=graph_inputs, outputs=graph_outputs, queue=False)
        ask_entity_button.click(
            fn=ask_about_entity,
            inputs=[entity_question, graph_doc],
            outputs=[message_box, scope_dd, graph_status2],
            queue=False,
        )
        graph_detail_button.click(
            fn=show_detail,
            inputs=[graph_doc],
            outputs=[graph_detail_html],
            queue=False,
        )

        demo.load(
            fn=load_documents,
            inputs=[status_filter],
            outputs=[document_table, document_picker, status_message],
            queue=False,
            show_progress="hidden",
        )
        demo.load(
            fn=_load_chat_sidebars,
            outputs=[scope_dd, conversation_dd],
            queue=False,
            show_progress="hidden",
        )
        demo.load(
            fn=lambda: gr.update(choices=_graph_document_choices(), value=""),
            outputs=[graph_doc],
            queue=False,
            show_progress="hidden",
        )
        demo.load(
            fn=load_graph,
            inputs=graph_inputs,
            outputs=graph_outputs,
            queue=False,
            show_progress="hidden",
        )

    return demo


def switch_view(name: str | None):
    """左侧导航切换视图，来源栏随问答视图一起显隐。"""
    return (
        gr.update(visible=name != "知识库" and name != "知识图谱"),
        gr.update(visible=name == "知识库"),
        gr.update(visible=name == "知识图谱"),
    )


def _conversation_titles() -> list[tuple[str, str]]:
    from app.services import conversations as conversation_service

    try:
        with session_scope() as session:
            items = conversation_service.list_conversations(session, limit=50)
        return [(item.title or "未命名", item.id) for item in items]
    except Exception:
        return []


def mount_app(app, path: str = "/"):
    """把 Gradio 界面挂到 FastAPI（Gradio 6 的 theme/css 需在挂载时传入）。"""
    return gr.mount_gradio_app(
        app,
        build_interface(),
        path=path,
        theme=_theme(),
        css=_css(),
        js=UI_JS,
        head='<meta name="color-scheme" content="light" />',
    )


def _theme() -> gr.themes.Base:
    return gr.themes.Base(
        primary_hue="blue",
        neutral_hue="gray",
        font=["-apple-system", "PingFang SC", "Microsoft YaHei", "sans-serif"],
    )


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
