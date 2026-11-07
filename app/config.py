"""应用配置：集中读取环境变量，禁止在代码中硬编码密钥、模型名与开发机路径。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """从 .env / 环境变量读取的全部运行配置。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "个人知识管理智能体"
    app_version: str = "0.1.0"
    environment: str = "dev"

    # 数据层
    database_url: str = "mysql+pymysql://pkma:changeme@127.0.0.1:3306/pkma?charset=utf8mb4"
    auto_create_tables: bool = True

    # 生成模型（OpenAI 兼容接口）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 90.0
    # 对话与抽取分开：对话要自然，抽取要稳定
    llm_temperature_chat: float = 0.5
    llm_temperature_extract: float = 0.15
    llm_max_tokens_chat: int = 1024
    # 抽取一批 8 个切片最多 ~28 实体 + 19 关系，800 会截断 JSON，这里给足
    llm_max_tokens_extract: int = 1200
    # disabled 时向接口传 thinking={"type":"disabled"}，首字从 ~15s 降到 ~2s；
    # 换用不支持该参数的厂商时设为 auto（不传该字段）
    llm_thinking: str = "disabled"

    # 向量模型
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "auto"
    embedding_batch_size: int = 32
    embedding_threads: int = 4
    hf_endpoint: str = "https://hf-mirror.com"
    hf_home: Path | None = None
    chroma_collection: str = "pkma_chunks_v1"

    # 存储路径
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path | None = None
    chroma_dir: Path | None = None

    # 入库与检索参数
    max_upload_mb: int = 40

    # ---- 入库闸门（2026-11-05 调整）----
    # 主闸门用「总字数」而不是切片数：切片数受断句影响，同样字数能差 20%，字数才是稳定的成本量。
    # 300 万字 = CLRS 这类千页教材（实测 244 万字）+ 23% 余量。
    # 实测 BGE-small 在本机 RTX 4060 上约 0.7 万字/秒（含分批与写库），
    # 244 万字 ≈ 5500 片 ≈ 6 分钟入库；调到 450 万则约 11 分钟。时间随字数线性增长。
    max_document_chars: int = 3_000_000
    # 1.0–1.5 倍：返回提示，带 force=true 可继续；>1.5 倍才拒绝
    max_document_chars_hard: int = 4_500_000
    # 页数只提示不拦截：扫描件页多字少、密排书页少字多，页数不代表内容量
    max_document_pages_warn: int = 1200
    # 切片数只是护栏：防的是有人把 CHUNK_SIZE 调到 100 导致切片爆炸，不是常规拒绝条件。
    # 实测约 442 字/片（含 80 字重叠），300 万字 ≈ 6800 片，12000 给到约 530 万字的余量
    max_document_chunks: int = 12_000
    # 扫描件判定：解析后「字数 ÷ 页数 < 50」基本可以确认是图片页；正常技术书页均 1400+ 字
    scanned_chars_per_page: float = 50
    # 并发上限：一次传多个文件会起多个线程抢同一块 GPU，实测并发反而更慢
    max_concurrent_ingest: int = 1
    allowed_extensions: str = "pdf,docx,txt"
    chunk_size: int = 500
    chunk_overlap: int = 80
    # 切片单位：token（推荐）或 char（旧行为）。
    # 英文 500 字符 ≈ 67 词，模型拿到的上下文太短；512 token ≈ 380 词，接近一段完整论述。
    # 中文 480 token ≈ 700–800 字；留 32 token 余量，避免按句累计后超过 BGE 的 512 被截断。
    chunk_unit: str = "token"
    chunk_tokens: int = 480
    chunk_overlap_tokens: int = 64
    # 父子分块：子片 160 token 负责向量匹配（定义句不会被稀释），
    # 父块 480 token（= chunk_tokens）给模型做上下文
    parent_child_enabled: bool = True
    chunk_child_tokens: int = 160
    chunk_child_overlap: int = 32
    # 查询理解：一次 LLM 调用拆实体/属性并补英文关键词，约 ¥0.0005/次（按问题缓存）
    query_understanding_enabled: bool = True
    # 旧字段保留为兼容别名，实际判定已改为按字数（见下方 max_document_chars 系列）
    max_document_pages: int = 1200
    ingest_batch_size: int = 128
    retrieval_top_k: int = 8
    context_top_k: int = 5
    relevance_threshold: float = 0.45
    # 候选池与重排：先多召回，再用「向量 + 关键词」融合排序
    retrieval_pool: int = 30
    # 灰色区间：低于阈值但高于此值，允许“部分回答”并声明不确定
    partial_floor: float = 0.32
    # 句子级引用对齐：把整段引用细化到“命中句”，多花约 50ms
    sentence_cite_enabled: bool = True
    # 混合检索：向量 + BM25 融合；关键词权重 0.3 表示 0.7 向量 + 0.3 关键词
    hybrid_enabled: bool = True
    hybrid_keyword_weight: float = 0.3
    # HyDE：先让模型写一段假设答案再检索（多一次 LLM 调用，默认关闭，见 README 对比数据）
    hyde_enabled: bool = False
    # 交叉编码器重排：需要下载 bge-reranker-base（约 1.1GB），默认关闭
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"
    # 交叉编码器分数（sigmoid 后）的判定档：相关通常 0.9+，无关 0.0–0.2
    rerank_high: float = 0.5
    rerank_partial: float = 0.2
    # 重排打开但模型加载失败时的兜底阈值：bge-m3 的分数被压缩，
    # 沿用 0.45 会把「红烧肉怎么做」判成有依据，所以这里要明显更高。
    rerank_missing_floor: float = 0.60
    graph_expansion_enabled: bool = True
    # 入库完成后自动做摘要/标签/实体关系；关闭后可通过接口手动触发
    enrich_on_ingest: bool = True

    # 发布
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("data_dir", "upload_dir", "chroma_dir", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def resolved_data_dir(self) -> Path:
        path = Path(self.data_dir).expanduser()
        return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()

    @property
    def resolved_upload_dir(self) -> Path:
        if not self.upload_dir:
            return self.resolved_data_dir / "uploads"
        path = Path(self.upload_dir).expanduser()
        return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()

    @property
    def resolved_chroma_dir(self) -> Path:
        """向量库目录。

        默认放在用户目录下的 ASCII 路径：Chroma 的本地引擎在 Windows 含中文的
        路径里写入较大索引时会丢失 HNSW 数据文件（元数据还在，索引无法加载）。
        需要放到别处时用 CHROMA_DIR 指定，且应保证是纯英文路径。
        """
        if not self.chroma_dir:
            return (Path.home() / ".pkma" / "chroma").resolve()
        path = Path(self.chroma_dir).expanduser()
        return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()

    @property
    def samples_dir(self) -> Path:
        return self.resolved_data_dir / "samples"

    @property
    def evaluation_dir(self) -> Path:
        return self.resolved_data_dir / "evaluation"

    @property
    def resolved_hf_home(self) -> Path:
        """向量模型缓存目录，默认放项目 data 目录，避免占用 C 盘。"""
        if not self.hf_home:
            return self.resolved_data_dir / "models"
        path = Path(self.hf_home).expanduser()
        return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()

    @property
    def allowed_extension_set(self) -> set[str]:
        return {f".{item.strip().lower().lstrip('.')}" for item in self.allowed_extensions.split(",") if item.strip()}

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    def ensure_directories(self) -> None:
        for directory in (
            self.resolved_data_dir,
            self.resolved_upload_dir,
            self.resolved_chroma_dir,
            self.samples_dir,
            self.evaluation_dir,
            self.resolved_hf_home,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def safe_database_url(self) -> str:
        """隐藏密码后的数据库地址，用于日志与健康检查展示。"""
        url = self.database_url
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        credentials, host_part = rest.split("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host_part}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
