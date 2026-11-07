"""SQLAlchemy 数据模型：文档、切片、会话、消息与图谱。

字段设计与《独立实施方案》5.1 节一致；主键统一为 UUID 字符串，
图谱数据按文档保存，便于删除与来源追溯。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32))
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # 知识组织（摘要/分类/标签/图谱）的结果状态，与文档入库状态分开记录
    enrichment_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    enrichment_error: Mapped[str | None] = mapped_column(Text)
    # 最近一次图谱抽取的采样片数与档位，用于界面如实展示覆盖比例
    graph_sampled_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    graph_depth: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )
    nodes: Mapped[list[GraphNode]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    edges: Mapped[list[GraphEdge]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_documents_status_created", "status", "created_at"),)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 父子分块：子片负责向量匹配，父块负责给模型上下文。
    # 父块的 parent_id 为空且 is_parent=True；子片指向父块 id。
    parent_id: Mapped[str | None] = mapped_column(String(36), index=True)
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(80), nullable=False, default="新对话")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="knowledge")
    document_scope_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="knowledge")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    citations_json: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    # 微秒精度：同一轮问答的用户/助手消息常在同一秒内写入，需要可稳定排序
    created_at: Mapped[datetime] = mapped_column(
        DateTime().with_variant(MySQLDateTime(fsp=6), "mysql"), default=utc_now, nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class GraphNode(Base):
    __tablename__ = "graph_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="概念")
    source_chunk_id: Mapped[str | None] = mapped_column(String(36), index=True)

    document: Mapped[Document] = relationship(back_populates="nodes")

    __table_args__ = (
        UniqueConstraint("document_id", "normalized_name", name="uq_node_doc_name"),
    )


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    source_chunk_id: Mapped[str | None] = mapped_column(String(36), index=True)

    document: Mapped[Document] = relationship(back_populates="edges")

    __table_args__ = (
        UniqueConstraint(
            "document_id", "source_node_id", "target_node_id", "relation", name="uq_edge_doc_relation"
        ),
    )
