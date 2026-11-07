"""文档入库：校验、去重、解析、切片、向量化、落库与清理。

一致性策略（实施方案 5.3）：
- 同一内容哈希只保留一份有效记录；
- 重建/重索引先按文档清理旧向量与切片，再写新数据；
- 任一环节失败即标记 failed 并保留可读原因，不留下 ready 假象。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_scope
from app.integrations import embeddings, vector_store
from app.models import Document, DocumentChunk, GraphEdge, GraphNode
from app.schemas import AppError
from app.services.chunking import (
    Chunk,
    ChunkConfig,
    chunk_document,
    chunk_document_hierarchical,
)
from app.services.parsing import parse_document

logger = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_upload(filename: str, size: int) -> str:
    """校验扩展名与大小，返回规范化扩展名。"""
    name = Path(filename or "").name
    if not name or name.startswith("."):
        raise AppError("invalid_filename", "文件名为空或非法", status_code=400)
    extension = Path(name).suffix.lower()
    if extension not in settings.allowed_extension_set:
        allowed = "、".join(sorted(settings.allowed_extension_set))
        raise AppError("unsupported_format", f"仅支持 {allowed} 格式", status_code=415)
    if size <= 0:
        raise AppError("empty_file", "文件为空", status_code=400)
    if size > settings.max_upload_bytes:
        raise AppError(
            "file_too_large",
            f"文件超过 {settings.max_upload_mb} MB 上限",
            status_code=413,
        )
    return extension


def save_upload(source: Path | str, original_name: str) -> tuple[Path, str]:
    """把临时文件复制到上传目录，返回存储路径与内容哈希。"""
    source_path = Path(source)
    original_name = Path(original_name).name
    extension = validate_upload(original_name, source_path.stat().st_size)
    content_hash = compute_sha256(source_path)

    settings.resolved_upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{extension}"
    target = settings.resolved_upload_dir / stored_name
    shutil.copyfile(source_path, target)
    return target, content_hash


def find_by_hash(session: Session, content_hash: str) -> Document | None:
    """按内容哈希查重，供接口层判断重复上传。"""
    return session.scalar(select(Document).where(Document.content_hash == content_hash))


def find_by_name(session: Session, original_name: str) -> Document | None:
    """按文件名查同一条记录，用于识别“同名但内容不同”的重传。"""
    return session.scalar(
        select(Document).where(Document.original_name == original_name).order_by(Document.created_at.desc())
    )


def register_upload(
    temp_path: Path | str,
    original_name: str,
    *,
    overwrite: bool = False,
) -> Document:
    """校验 → 查重 → 登记文档记录（状态 processing），供接口层与界面层共用。

    两级查重：
    1. 内容哈希相同：直接判重复（duplicate_document），避免重复索引；
    2. 文件名相同但内容不同：判 duplicate_name，需显式 overwrite=True 才覆盖，
       覆盖时先删旧记录（文件、切片、向量、图谱一起清理），再按新内容入库。

    失败记录会被替换，便于用户重试。
    """
    temp_path = Path(temp_path)
    name = Path(original_name).name or "upload"
    extension = validate_upload(name, temp_path.stat().st_size)
    stored_path, content_hash = save_upload(temp_path, name)
    obsolete: dict[str, Path] = {}

    try:
        with session_scope() as session:
            same_hash = find_by_hash(session, content_hash)
            if same_hash is not None and same_hash.status != "failed":
                raise AppError(
                    "duplicate_document",
                    "该文件内容已存在，未重复入库",
                    status_code=409,
                    detail={"document_id": same_hash.id, "name": same_hash.original_name},
                )
            if same_hash is not None:
                session.delete(same_hash)
                session.flush()

            same_name = find_by_name(session, name)
            if same_name is not None and same_name.status != "failed":
                if not overwrite:
                    raise AppError(
                        "duplicate_name",
                        f"已存在同名文档《{name}》，如需替换请勾选“覆盖同名文档”后重传",
                        status_code=409,
                        detail={"document_id": same_name.id, "name": same_name.original_name},
                    )
                existing_id = same_name.id
                existing_path = Path(same_name.stored_path)
                session.delete(same_name)
                session.flush()
                # 旧向量/图谱/文件等新记录提交后再清：否则新上传失败时，
                # 旧记录还在但文件已经没了
                obsolete[existing_id] = existing_path
                logger.info("覆盖同名文档：%s（旧 %s，待新记录提交后清理）", name, existing_id[:8])

            document = create_document(session, temp_path, name, stored_path, content_hash, extension)
            new_id = document.id

        # 事务已提交，旧文档的派生物现在才真正可以清
        if obsolete:
            _PENDING_PURGE[new_id] = list(obsolete.items())
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    return document



def purge_document_artifacts(
    document_id: str,
    *,
    file_path: str | Path | None = None,
    session: Session | None = None,
) -> None:
    """一份文档的全部派生物一起清掉：向量、切片、图谱节点/边、原文件。

    以前重索引只清向量与切片，图谱会残留指向已删除切片的节点和边；
    清理失败路径也只删这两样，同样留下孤儿图谱。
    """
    try:
        vector_store.delete_by_document(document_id)
    except Exception:  # pragma: no cover - 清理失败不应中断主流程
        logger.warning("清理向量失败：%s", document_id)

    def _clean_tables(target: Session) -> None:
        target.execute(delete(GraphEdge).where(GraphEdge.document_id == document_id))
        target.execute(delete(GraphNode).where(GraphNode.document_id == document_id))
        target.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))

    if session is not None:
        _clean_tables(session)
    else:
        with session_scope() as fresh:
            _clean_tables(fresh)

    if file_path:
        Path(file_path).unlink(missing_ok=True)



def create_document(
    session: Session,
    source: Path | str,
    original_name: str,
    stored_path: Path,
    content_hash: str,
    extension: str,
) -> Document:
    document = Document(
        original_name=Path(original_name).name,
        stored_name=stored_path.name,
        stored_path=str(stored_path),
        extension=extension,
        size=Path(source).stat().st_size,
        content_hash=content_hash,
        status="processing",
        tags_json=[],
    )
    session.add(document)
    session.flush()
    return document


def _clear_document_data(session: Session, document: Document) -> None:
    purge_document_artifacts(document.id, session=session)


def _lower_thread_priority() -> None:
    """入库线程降到 BelowNormal，避免长时间占用 CPU 影响整机响应。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:  # pragma: no cover - 平台差异时忽略
        logger.debug("设置线程优先级失败", exc_info=True)


# 覆盖上传时的旧文档：新记录提交后才清理，避免"新失败、旧文件已删"
_PENDING_PURGE: dict[str, list[tuple[str, Path]]] = {}
_PENDING_LOCK = threading.Lock()


def _drain_pending_purge(new_document_id: str) -> None:
    with _PENDING_LOCK:
        pending = _PENDING_PURGE.pop(new_document_id, [])
    for old_id, old_path in pending:
        purge_document_artifacts(old_id, file_path=old_path)
        logger.info("已清理被覆盖文档的旧数据：%s", old_id[:8])


_INGEST_SLOTS = threading.BoundedSemaphore(max(1, settings.max_concurrent_ingest))


def probe_document(path: Path, extension: str | None = None) -> dict:
    """上传时快速探测页数与字数，用于闸门判断。

    PDF 350–560 页实测 0.3–0.6 秒，同步做一次不影响上传手感；
    代价是正式入库时会再解析一次，换来的是「超限当场拒绝」而不是入库到一半才失败。
    """
    # 先拿页数：即使一个字都提不出来，也要能告诉用户「几页的扫描件」
    quick_pages = 0
    if (extension or path.suffix).lower() == ".pdf":
        try:
            import pymupdf

            with pymupdf.open(path) as pdf:
                quick_pages = pdf.page_count
        except Exception:
            quick_pages = 0

    try:
        parsed = parse_document(path, extension)
    except AppError as exc:
        if exc.code == "empty_document":
            raise AppError(
                "scanned_document",
                f"疑似扫描件：{quick_pages or '多'} 页里没有可提取的文本。"
                "请先 OCR 成可复制文本再上传，否则切不出有效内容。",
                status_code=422,
            ) from exc
        raise

    pages = parsed.page_count or quick_pages
    chars = parsed.char_count
    return {
        "pages": pages,
        "chars": chars,
        # 没有页概念的格式（docx/txt）不做字/页判定，避免把短文误判成扫描件
        "chars_per_page": round(chars / pages, 1) if pages else None,
    }


def check_document_limits(probe: dict, force: bool = False) -> list[str]:
    """按「字数为主、页数只提示、切片数护栏」做三档判定，返回提示信息列表。

    超限抛 AppError：
    - 疑似扫描件 / 超 1.5 倍 / 切片护栏 → 直接拒绝
    - 1.0–1.5 倍且未 force → 409，由前端提示后可带 force=true 重传
    """
    warnings: list[str] = []
    chars = probe["chars"]
    pages = probe["pages"]

    per_page = probe.get("chars_per_page")
    if pages and per_page is not None and per_page < settings.scanned_chars_per_page:
        raise AppError(
            "scanned_document",
            f"疑似扫描件：{pages} 页仅 {chars} 字（约 {per_page:.0f} 字/页）。"
            "请先 OCR 成可复制文本再上传，否则切不出有效内容。",
            status_code=422,
        )

    if chars > settings.max_document_chars_hard:
        raise AppError(
            "document_too_large",
            f"文档约 {chars / 10000:.0f} 万字（预计 {round(chars / 442)} 片），"
            f"超过硬上限 {settings.max_document_chars_hard / 10000:.0f} 万字，请拆分后上传",
            status_code=413,
            detail={"chars": chars, "limit": settings.max_document_chars_hard},
        )

    if chars > settings.max_document_chars and not force:
        raise AppError(
            "document_large_confirm",
            f"文档约 {chars / 10000:.1f} 万字（预计 {round(chars / 442)} 片），超过建议上限 "
            f"{settings.max_document_chars / 10000:.0f} 万字，向量化预计需要 "
            f"{chars / 7000:.0f} 秒。确认继续请带 force 参数重传",
            status_code=409,
            detail={"chars": chars, "limit": settings.max_document_chars},
        )

    if chars > settings.max_document_chars and force:
        warnings.append(f"已按强制模式处理超建议上限的文档（{chars / 10000:.1f} 万字）")

    if pages > settings.max_document_pages_warn:
        warnings.append(f"文档 {pages} 页，超过提示阈值 {settings.max_document_pages_warn} 页，处理会较慢")

    return warnings


def ingest_document(document_id: str) -> None:
    """对已存在的文档记录执行解析→切片→向量化→入库。

    切片按批次写入数据库与向量库，文档列表中的切片数会随之增长，
    据此可以观察大文档的处理进度。
    """
    _lower_thread_priority()
    # 覆盖上传：新记录已提交，先清掉被替换那份的向量/图谱/文件
    _drain_pending_purge(document_id)

    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            logger.warning("文档不存在，跳过入库：%s", document_id)
            return
        document.status = "processing"
        document.error_message = None
        session.flush()
        path = Path(document.stored_path)
        extension = document.extension
        filename = document.original_name
        category = document.category

    try:
        parsed = parse_document(path, extension)
        # 页数只做提示，不拦截：扫描件页多字少、密排书页少字多，页数不代表内容量
        if parsed.page_count and parsed.page_count > settings.max_document_pages_warn:
            logger.info("文档页数较多：%s 页（仅提示，不拦截）", parsed.page_count)
        # 重试/重建索引不经过上传接口，这里再兜一次底
        if parsed.char_count > settings.max_document_chars_hard:
            raise AppError(
                "document_too_large",
                f"文档约 {parsed.char_count / 10000:.0f} 万字，超过硬上限"
                f"{settings.max_document_chars_hard / 10000:.0f} 万字",
                status_code=413,
            )
        if (
            parsed.page_count
            and parsed.char_count / max(1, parsed.page_count) < settings.scanned_chars_per_page
        ):
            raise AppError(
                "scanned_document",
                f"疑似扫描件：{parsed.page_count} 页仅 {parsed.char_count} 字，请先 OCR 后再入库",
                status_code=422,
            )

        config = ChunkConfig(
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
            unit=settings.chunk_unit,
            tokens=settings.chunk_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        # 父子分块：父块 480 token 给模型，子片 160 token 做向量匹配
        parent_groups: list[tuple[Chunk, list[Chunk]]] = []
        if settings.parent_child_enabled and config.unit == "token":
            parent_groups = chunk_document_hierarchical(
                parsed,
                config,
                child_tokens=settings.chunk_child_tokens,
                child_overlap=settings.chunk_child_overlap,
            )
            children = [child for _, group in parent_groups for child in group]
        else:
            children = chunk_document(parsed, config)

        if not children:
            raise AppError("empty_document", "文档没有可切片的有效文本", status_code=422)
        if len(children) > settings.max_document_chunks:
            raise AppError(
                "chunk_explosion",
                f"文档将产生 {len(children)} 个子片，超过护栏 {settings.max_document_chunks}。"
                "通常是 CHUNK_SIZE 被调得过小，请检查配置或拆分文档",
                status_code=413,
            )

        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                return
            _clear_document_data(session, document)

        chunks = children

        total = len(chunks)
        batch_size = max(1, settings.ingest_batch_size)

        # 向量化按批次进行并输出进度；写入 Chroma 必须一次性完成，
        # 多次小批次 upsert 会让 compactor 留下未完成的 backfill，导致索引无法加载。
        vectors: list[list[float]] = []
        for start in range(0, total, batch_size):
            batch = chunks[start : start + batch_size]
            vectors.extend(embeddings.encode_texts([chunk.content for chunk in batch]))
        if len(vectors) != total:
            raise RuntimeError("向量数量与切片数量不一致")

        records: list[vector_store.VectorRecord] = []
        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise AppError("document_not_found", "文档在入库过程中被删除", status_code=409)

            # 父块先落库，子片再挂 parent_id；父块不进向量库（只给模型看）
            parent_ids: list[str] = []
            if parent_groups:
                for order, (parent, _) in enumerate(parent_groups):
                    parent_id = str(uuid.uuid4())
                    parent_ids.append(parent_id)
                    session.add(
                        DocumentChunk(
                            id=parent_id,
                            document_id=document.id,
                            chunk_index=order,
                            page_start=parent.page_start,
                            page_end=parent.page_end,
                            content=parent.content,
                            content_hash=parent.content_hash,
                            is_parent=True,
                        )
                    )
                session.flush()

            child_cursor = 0
            for group_index, group in (enumerate(parent_groups) if parent_groups else []):
                group_size = len(group[1])
                for offset in range(group_size):
                    child = group[1][offset]
                    vector = vectors[child_cursor]
                    child_cursor += 1
                    chunk_id = str(uuid.uuid4())
                    session.add(
                        DocumentChunk(
                            id=chunk_id,
                            document_id=document.id,
                            chunk_index=len(parent_groups) + child_cursor - 1,
                            page_start=child.page_start,
                            page_end=child.page_end,
                            content=child.content,
                            content_hash=child.content_hash,
                            parent_id=parent_ids[group_index],
                        )
                    )
                    records.append(
                        vector_store.VectorRecord(
                            id=chunk_id,
                            vector=vector,
                            document=child.content,
                            metadata={
                                "document_id": document.id,
                                "chunk_index": child.chunk_index,
                                "filename": filename,
                                "page_start": child.page_start,
                                "page_end": child.page_end,
                                "category": category or "",
                                "parent_id": parent_ids[group_index],
                            },
                        )
                    )

            flat_cursor = 0
            for chunk, vector in ([] if parent_groups else zip(chunks, vectors, strict=True)):
                chunk_id = str(uuid.uuid4())
                flat_cursor += 1
                session.add(
                    DocumentChunk(
                        id=chunk_id,
                        document_id=document.id,
                        chunk_index=chunk.chunk_index,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        content=chunk.content,
                        content_hash=chunk.content_hash,
                    )
                )
                records.append(
                    vector_store.VectorRecord(
                        id=chunk_id,
                        vector=vector,
                        document=chunk.content,
                        metadata={
                            "document_id": document.id,
                            "chunk_index": chunk.chunk_index,
                            "filename": filename,
                            "page_start": chunk.page_start,
                            "page_end": chunk.page_end,
                            "category": category or "",
                        },
                    )
                )

        # Chroma 单批上限 5461；大文档（万余子片）必须分批，否则报 Batch size 超限
        vector_store.upsert(records, batch_size=min(2000, max(256, len(records))))
        logger.info("入库进度：%s 向量已写入 %d/%d", filename, len(records), total)

        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is not None:
                document.status = "ready"
                document.error_message = None
                document.enrichment_status = "pending"
                document.updated_at = datetime.now()
        logger.info("文档入库完成：%s（%d 个切片）", filename, total)

        # 入库与知识组织解耦：文档先可用，摘要/标签/图谱在后台补
        if settings.enrich_on_ingest and settings.llm_configured:
            from app.services import enrichment

            enrichment.start_enrich(document_id)
    except Exception as exc:
        message = exc.message if isinstance(exc, AppError) else f"{type(exc).__name__}: {exc}"
        logger.exception("文档入库失败：%s", document_id)
        _fail_document(document_id, message)


def _fail_document(document_id: str, message: str) -> None:
    try:
        purge_document_artifacts(document_id)
    except Exception:  # pragma: no cover - 清理失败不应掩盖原始错误
        logger.warning("清理失败文档的派生物失败：%s", document_id)
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is not None:
            document.status = "failed"
            document.error_message = message[:500]
            document.updated_at = datetime.now()


def _run_ingest(document_id: str) -> None:
    """排队执行：并发上限内才真正跑，避免多个文件同时抢同一块 GPU。"""
    with _INGEST_SLOTS:
        ingest_document(document_id)


def start_ingest(document_id: str) -> threading.Thread:
    """后台执行入库，不引入独立任务队列。"""
    thread = threading.Thread(
        target=_run_ingest, args=(document_id,), daemon=True, name=f"ingest-{document_id[:8]}"
    )
    thread.start()
    return thread


def delete_document(document_id: str) -> None:
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise AppError("document_not_found", "文档不存在", status_code=404)
        stored_path = Path(document.stored_path)
        vector_store.delete_by_document(document.id)
        session.delete(document)
    if stored_path.exists():
        stored_path.unlink(missing_ok=True)


def reindex_document(document_id: str) -> threading.Thread:
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise AppError("document_not_found", "文档不存在", status_code=404)
        if not Path(document.stored_path).exists():
            document.status = "failed"
            document.error_message = "原始文件已丢失，无法重建索引"
            raise AppError("file_missing", "原始文件已丢失，无法重建索引", status_code=409)
        document.status = "processing"
        document.error_message = None
    return start_ingest(document_id)


def recover_interrupted(session: Session) -> int:
    """应用重启后，把仍停在 processing 的文档标记为可重试。

    中断的文档通常已经写了一半切片/向量/图谱，只改状态会留下孤儿数据，
    所以顺带把派生物清干净，让「重新索引」从零开始。
    """
    stale = session.scalars(select(Document).where(Document.status == "processing")).all()
    for document in stale:
        purge_document_artifacts(document.id, session=session)
        document.status = "failed"
        document.error_message = "应用重启导致处理中断，请重新索引"
    return len(stale)


def chunk_count(session: Session, document_id: str) -> int:
    return int(
        session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id))
        or 0
    )
