"""文档接口：上传、列表、详情、重新索引、删除。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Document, DocumentChunk
from app.schemas import AppError, ChunkOut, DocumentDetail, DocumentListResponse, DocumentOut
from app.services import ingestion

router = APIRouter(prefix="/api/documents", tags=["documents"])

STATUS_LABELS = {"processing": "处理中", "ready": "就绪", "failed": "失败"}


def _to_out(document: Document, chunk_count: int) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        original_name=document.original_name,
        extension=document.extension,
        size=document.size,
        status=document.status,
        error_message=document.error_message,
        summary=document.summary,
        category=document.category,
        tags=list(document.tags_json or []),
        enrichment_status=document.enrichment_status or "pending",
        enrichment_error=document.enrichment_error,
        chunk_count=chunk_count,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def list_documents(
    session: Session,
    status: str | None = None,
    category: str | None = None,
    limit: int = 100,
) -> tuple[list[DocumentOut], dict[str, int]]:
    stmt = select(Document).order_by(Document.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Document.status == status)
    if category:
        stmt = stmt.where(Document.category == category)
    documents = session.scalars(stmt).all()

    counts = dict(
        session.execute(select(Document.status, func.count()).group_by(Document.status)).all()  # type: ignore[arg-type]
    )
    chunk_counts = dict(
        session.execute(
            select(DocumentChunk.document_id, func.count())
            .where(DocumentChunk.is_parent.is_(False))
            .group_by(DocumentChunk.document_id)
        ).all()  # type: ignore[arg-type]
    )
    items = [_to_out(document, int(chunk_counts.get(document.id, 0))) for document in documents]
    return items, counts


@router.get("", response_model=DocumentListResponse, summary="文档列表")
def get_documents(
    status: str | None = Query(default=None, description="processing / ready / failed"),
    category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> DocumentListResponse:
    items, counts = list_documents(session, status, category, limit)
    return DocumentListResponse(items=items, total=len(items), counts=counts)


@router.post("", response_model=DocumentOut, status_code=201, summary="上传文档并开始入库")
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    overwrite: bool = Form(default=False, description="同名文档是否覆盖"),
    force: bool = Form(default=False, description="超过建议字数时是否强制继续"),
) -> DocumentOut:
    settings.ensure_directories()
    temp_dir = settings.resolved_upload_dir / ".incoming"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{Path(file.filename or 'upload').name}"

    size = 0
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise AppError("file_too_large", f"文件超过 {settings.max_upload_mb} MB 上限", status_code=413)
                handle.write(chunk)
    finally:
        await file.close()

    try:
        ingestion.validate_upload(file.filename or "", size)
        if size == 0:
            raise AppError("empty_file", "文件为空", status_code=400)

        # 解析成本很低（350–560 页约 0.5 秒），同步做一次就能在上传时给出明确判断，
        # 而不是等入库跑一半才失败
        probe = ingestion.probe_document(temp_path, Path(file.filename or "").suffix or None)
        warnings = ingestion.check_document_limits(probe, force=force)
        if warnings:
            # HTTP 头只允许 latin-1，中文提示必须百分号编码；前端 decodeURIComponent 还原
            response.headers["X-Ingest-Warning"] = quote(" | ".join(warnings), safe="")

        document = ingestion.register_upload(temp_path, file.filename or "upload", overwrite=overwrite)
        document_id = document.id
        payload = _to_out(document, 0)
    finally:
        temp_path.unlink(missing_ok=True)

    ingestion.start_ingest(document_id)
    return payload


@router.post("/retry-failed", summary="批量重试失败的文档")
def retry_failed(session: Session = Depends(get_db)) -> dict:
    from sqlalchemy import select as sa_select

    from app.services import enrichment

    failed = list(session.scalars(sa_select(Document).where(Document.status == "failed")).all())
    retried: list[str] = []
    for document in failed:
        if not Path(document.stored_path).exists():
            document.error_message = "原始文件已丢失，无法重试，请重新上传"
            continue
        document.status = "processing"
        document.error_message = None
        session.commit()
        ingestion.start_ingest(document.id)
        retried.append(document.id)

    # 入库成功但知识组织失败的，一并补做
    enrich_pending = list(
        session.scalars(
            sa_select(Document).where(Document.status == "ready", Document.enrichment_status == "failed")
        ).all()
    )
    for document in enrich_pending:
        enrichment.start_enrich(document.id)

    return {
        "retried": len(retried),
        "enrich_retried": len(enrich_pending),
        "skipped": len(failed) - len(retried),
        "document_ids": retried,
    }


@router.get("/{document_id}", response_model=DocumentDetail, summary="文档详情")
def get_document(document_id: str, session: Session = Depends(get_db)) -> DocumentDetail:
    document = session.get(Document, document_id)
    if document is None:
        raise AppError("document_not_found", "文档不存在", status_code=404)
    chunks = session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id, DocumentChunk.is_parent.is_(False))
        .order_by(DocumentChunk.chunk_index)
        .limit(200)
    ).all()
    # 切片明细只返回前 200 条用于预览，切片总数按数据库真实计数，不能被截断
    base = _to_out(document, ingestion.chunk_count(session, document_id))
    return DocumentDetail(
        **base.model_dump(),
        chunks=[ChunkOut.model_validate(chunk) for chunk in chunks],
    )


@router.post("/{document_id}/reindex", response_model=DocumentOut, summary="按文档重建索引")
def reindex_document(document_id: str, session: Session = Depends(get_db)) -> DocumentOut:
    document = session.get(Document, document_id)
    if document is None:
        raise AppError("document_not_found", "文档不存在", status_code=404)
    session.rollback()  # 结束只读事务，避免与后台重建使用的会话快照不一致
    ingestion.reindex_document(document_id)
    session.expire_all()
    refreshed = session.get(Document, document_id)
    return _to_out(refreshed, ingestion.chunk_count(session, document_id))


@router.post("/{document_id}/enrich", response_model=DocumentOut, summary="生成摘要、分类、标签与图谱")
def enrich_document(
    document_id: str,
    depth: str = Query(default="standard", pattern="^(fast|standard|deep)$"),
    session: Session = Depends(get_db),
) -> DocumentOut:
    from app.services import enrichment

    document = session.get(Document, document_id)
    if document is None:
        raise AppError("document_not_found", "文档不存在", status_code=404)
    if document.status != "ready":
        raise AppError("document_not_ready", "文档尚未完成入库，无法生成知识组织", status_code=409)
    if not settings.llm_configured:
        raise AppError("llm_not_configured", "未配置生成模型，无法生成摘要与图谱", status_code=503)

    document.enrichment_status = "processing"
    document.enrichment_error = None
    session.commit()
    enrichment.start_enrich(document_id, depth=depth)
    return _to_out(document, ingestion.chunk_count(session, document_id))


@router.delete("/{document_id}", status_code=204, summary="删除文档及关联数据")
def remove_document(document_id: str, session: Session = Depends(get_db)) -> None:
    if session.get(Document, document_id) is None:
        raise AppError("document_not_found", "文档不存在", status_code=404)
    session.rollback()
    ingestion.delete_document(document_id)

@router.get("/{document_id}/file", summary="按原样返回文档（用于跳页阅读）")
def serve_document(document_id: str, session: Session = Depends(get_db)) -> FileResponse:
    document = session.get(Document, document_id)
    if document is None:
        raise AppError("document_not_found", "文档不存在", status_code=404)
    path = Path(document.stored_path)
    if not path.exists():
        raise AppError("file_missing", "文件已不在磁盘上", status_code=410)
    return FileResponse(
        path,
        media_type="application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )
