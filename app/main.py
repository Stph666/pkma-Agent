"""FastAPI 应用入口：路由注册、统一异常处理、Gradio 挂载。"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.api import chat, chunks, conversations, documents, graph, health, search
from app.config import settings
from app.db import init_db, table_names
from app.schemas import AppError
from app.ui.app import mount_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    if settings.auto_create_tables:
        # 数据库不可用时不阻塞启动：界面仍可打开，健康检查会明确报告 database 异常
        try:
            init_db()
            logger.info("数据表检查完成，共 %d 张表", len(table_names()))
            from app.db import session_scope
            from app.services.ingestion import recover_interrupted

            with session_scope() as session:
                recovered = recover_interrupted(session)
            if recovered:
                logger.warning("发现 %d 份文档停留在处理中，已标记为失败并允许重新索引", recovered)
        except Exception as exc:
            logger.error("数据表初始化失败，应用以只读降级方式启动：%s", exc)
    # 后台预热向量模型：否则重启后的第一个提问要多等约 10 秒
    def _warmup() -> None:
        try:
            from app.integrations import embeddings

            embeddings.encode_query("预热")
            logger.info("向量模型预热完成")

            # 重排模型首次加载要十几秒，顺手一起预热，避免答辩时第一个问题卡住
            if settings.rerank_enabled:
                from app.integrations import reranker

                if reranker.get_model() is not None:
                    reranker.get_model().predict([("预热", "预热文本")])
                    logger.info("重排模型预热完成")
        except Exception as exc:  # pragma: no cover - 预热失败不影响主流程
            logger.warning("向量模型预热失败：%s", exc)

    threading.Thread(target=_warmup, daemon=True, name="embedding-warmup").start()

    logger.info("应用启动：%s v%s（%s）", settings.app_name, settings.app_version, settings.environment)
    yield
    logger.info("应用关闭")


def settings_base_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_request",
                "message": "请求参数不合法",
                "detail": {"errors": exc.errors()[:5]},
            },
        )

    @app.exception_handler(OperationalError)
    async def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("数据库不可用：%s", str(exc).splitlines()[0])
        return JSONResponse(
            status_code=503,
            content={
                "code": "database_unavailable",
                "message": "数据库不可用，请确认 MySQL 已启动且账号密码正确",
                "detail": None,
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("数据库操作失败：%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": "database_error", "message": "数据库操作失败，请稍后重试", "detail": None},
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "服务器内部错误，请稍后重试", "detail": None},
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="基于 LLM 的个人知识管理智能体（课程设计第 22 题）",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(graph.router)
    app.include_router(search.router)
    app.include_router(chunks.router)

    # 图谱页需要的 ECharts 走本地静态资源，避免依赖 CDN
    assets_dir = settings_base_dir() / "app" / "ui" / "assets"
    if assets_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(assets_dir)), name="static")

    # 新版前端（React）挂根路径，Gradio 保留在 /classic 作为兜底
    dist_dir = settings_base_dir() / "web" / "dist"
    if dist_dir.is_dir():
        app = mount_app(app, path="/classic")
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="web")
        return app
    return mount_app(app)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
