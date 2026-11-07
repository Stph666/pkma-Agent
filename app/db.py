"""数据库连接与建表入口。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url
        connect_args: dict[str, object] = {}
        if url.startswith("mysql"):
            # 避免数据库不可达时请求长时间挂起
            connect_args = {"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30}
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def reset_engine() -> None:
    """测试或多环境切换时释放现有连接池。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入使用的会话。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """按 ORM 模型创建缺失的表，并为已有表补齐新增字段。"""
    from app import models  # noqa: F401  确保模型已注册到 metadata

    Base.metadata.create_all(bind=get_engine())
    _ensure_columns()
    _ensure_timestamp_precision()


def _ensure_columns() -> None:
    """create_all 不会给已存在的表加字段，这里做一次幂等补列。"""
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(get_engine())
    if "documents" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("documents")}
    additions = {
        "enrichment_status": "VARCHAR(16) NOT NULL DEFAULT 'pending'",
        "enrichment_error": "TEXT NULL",
        "graph_sampled_chunks": "INT NOT NULL DEFAULT 0",
        "graph_depth": "VARCHAR(16) NOT NULL DEFAULT ''",
    }
    missing = {name: ddl for name, ddl in additions.items() if name not in existing}
    if missing:
        with get_engine().begin() as connection:
            for name, ddl in missing.items():
                connection.execute(text(f"ALTER TABLE documents ADD COLUMN {name} {ddl}"))
        logger.info("已补齐 documents 表字段：%s", ", ".join(missing))

    # 父子分块需要的两个字段
    if "document_chunks" in inspector.get_table_names():
        chunk_columns = {column["name"] for column in inspector.get_columns("document_chunks")}
        chunk_additions = {
            "parent_id": "VARCHAR(36) NULL",
            "is_parent": "TINYINT(1) NOT NULL DEFAULT 0",
        }
        chunk_missing = {
            name: ddl for name, ddl in chunk_additions.items() if name not in chunk_columns
        }
        if chunk_missing:
            with get_engine().begin() as connection:
                for name, ddl in chunk_missing.items():
                    connection.execute(text(f"ALTER TABLE document_chunks ADD COLUMN {name} {ddl}"))
                connection.execute(
                    text("CREATE INDEX ix_document_chunks_parent_id ON document_chunks (parent_id)")
                )
            logger.info("已补齐 document_chunks 表字段：%s", ", ".join(chunk_missing))


def _ensure_timestamp_precision() -> None:
    """messages.created_at 需要微秒精度，否则同秒消息顺序不稳定。"""
    if not settings.database_url.startswith("mysql"):
        return
    with get_engine().begin() as connection:
        precision = connection.execute(
            text(
                "SELECT DATETIME_PRECISION FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'messages' AND column_name = 'created_at'"
            )
        ).scalar()
        if precision in (0, None):
            connection.execute(text("ALTER TABLE messages MODIFY created_at DATETIME(6) NOT NULL"))
            logger.info("messages.created_at 已升级为微秒精度")


def check_database() -> tuple[bool, str]:
    """健康检查用：返回连接是否可用与说明。"""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "数据库连接正常"
    except Exception as exc:  # pragma: no cover - 依赖真实数据库状态
        logger.warning("数据库健康检查失败: %s", exc)
        return False, f"数据库连接失败：{type(exc).__name__}"


def table_names() -> list[str]:
    from app import models  # noqa: F401

    return sorted(Base.metadata.tables.keys())
