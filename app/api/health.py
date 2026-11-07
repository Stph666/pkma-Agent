"""健康检查接口：应用、数据库、向量库与模型配置。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.config import settings
from app.db import check_database
from app.integrations import embeddings
from app.schemas import ComponentStatus, HealthResponse

router = APIRouter(prefix="/api", tags=["health"])


def _llm_status() -> ComponentStatus:
    if settings.llm_configured:
        return ComponentStatus(ok=True, detail=f"已配置模型 {settings.llm_model}")
    return ComponentStatus(ok=False, detail="未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")


def _vector_status() -> ComponentStatus:
    chroma_dir = settings.resolved_chroma_dir
    device = embeddings.device_info()
    if chroma_dir.exists():
        return ComponentStatus(ok=True, detail=f"向量目录可用｜推理设备 {device}")
    return ComponentStatus(ok=False, detail=f"向量目录未创建｜推理设备 {device}")


def collect_health() -> HealthResponse:
    db_ok, db_detail = check_database()
    llm = _llm_status()
    vector = _vector_status()
    components = [db_ok, llm.ok, vector.ok]
    return HealthResponse(
        status="ok" if all(components) else "degraded",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        server_time=datetime.now(),
        database=ComponentStatus(ok=db_ok, detail=db_detail),
        llm=llm,
        vector_store=vector,
    )


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health() -> HealthResponse:
    return collect_health()
