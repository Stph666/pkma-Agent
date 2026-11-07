"""Pydantic 请求与响应模型。阶段 A 仅包含健康检查与统一错误结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ComponentStatus(BaseModel):
    ok: bool
    detail: str


class HealthResponse(BaseModel):
    status: str = Field(description="ok 表示全部关键组件可用，degraded 表示部分降级")
    app_name: str
    version: str
    environment: str
    server_time: datetime
    database: ComponentStatus
    llm: ComponentStatus
    vector_store: ComponentStatus


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict | None = None


class AppError(Exception):
    """业务异常：由统一异常处理器转换为稳定错误码。"""

    def __init__(self, code: str, message: str, status_code: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chunk_index: int
    page_start: int | None = None
    page_end: int | None = None
    content: str
    content_hash: str


class DocumentOut(BaseModel):
    id: str
    original_name: str
    extension: str
    size: int
    status: str
    error_message: str | None = None
    summary: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    enrichment_status: str = "pending"
    enrichment_error: str | None = None
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentOut):
    chunks: list[ChunkOut] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    total: int
    counts: dict[str, int] = Field(default_factory=dict)


class ConversationOut(BaseModel):
    id: str
    title: str
    mode: str
    message_count: int = 0
    document_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    mode: str
    status: str
    citations: list[dict] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationOut]
    total: int


class ConversationCreate(BaseModel):
    title: str | None = None
    mode: str = Field(default="knowledge", pattern="^(knowledge|general)$")
    document_ids: list[str] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    document_ids: list[str] | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    mode: str = Field(default="knowledge", pattern="^(knowledge|general)$")
    document_ids: list[str] = Field(default_factory=list)
    # 重新生成：复用上一条用户消息，删掉最后一条助手回答后重跑
    regenerate: bool = False


class GraphNodeOut(BaseModel):
    id: str
    name: str
    type: str
    document_id: str
    filename: str = ""
    location: str = ""
    snippet: str = ""
    content: str = ""
    degree: int = 0


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    relation: str
    relation_count: int = 1
    relations: list[str] = Field(default_factory=list)
    label: str = ""
    document_id: str
    location: str = ""
    snippet: str = ""
    content: str = ""
    source_name: str = ""
    target_name: str = ""


class GraphResponse(BaseModel):
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    total_entities: int = 0
    shown_entities: int = 0
    total_relations: int = 0
    shown_relations: int = 0
    hidden_isolated: int = 0
    truncated: bool = False
    # 关联度分布：high=关联≥3 / low=关联 1–2 / isolated=0，用于界面解释「为什么低关联的最多」
    degree_histogram: dict[str, int] = Field(default_factory=dict)
    # 图谱覆盖的切片数 / 范围内切片总数，用于如实展示抽样比例
    covered_chunks: int = 0
    total_chunks: int = 0
    # 最近一次抽取实际采样的切片数与档位
    sampled_chunks: int = 0
    depth: str = ""
    depth_label: str = ""
