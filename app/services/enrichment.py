"""知识组织：摘要、分类、标签与实体关系抽取。

- 摘要/分类/标签：单次调用生成，分类限定在固定集合内；
- 实体关系：对限量切片抽取三元组，保留来源切片便于追溯；
- 任一步失败只标记知识组织失败，不影响文档检索与问答。
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select

from app.config import settings
from app.db import session_scope
from app.integrations import llm
from app.models import Document, DocumentChunk, GraphEdge, GraphNode
from app.schemas import AppError
from app.services.parsing import normalize_text

logger = logging.getLogger(__name__)

CATEGORIES = ("课程资料", "技术文档", "研究论文", "工作资料", "个人笔记", "其他")

SUMMARY_BUDGET = 6000

# 图谱抽取按文档规模采样：大文档多抽、小文档少抽，并分批调用
GRAPH_BATCH_BUDGET = 4000       # 单批放入提示词的最大字符数
GRAPH_BATCH_CHUNKS = 8          # 单批最多切片数
GRAPH_MIN_CHUNKS = 8            # 小文档至少抽取的切片数
MAX_ENTITIES_PER_BATCH = 20
MAX_RELATIONS_PER_BATCH = 24
MAX_ENTITIES_TOTAL = 320
MAX_RELATIONS_TOTAL = 420

# 抽取档位：采样比例与切片上限一起放大，档位才有实际差别
DEPTH_PRESETS = {
    "fast": {"label": "快速", "ratio": 0.01, "max_chunks": 32},
    "standard": {"label": "标准", "ratio": 0.02, "max_chunks": 64},
    "deep": {"label": "深度", "ratio": 0.05, "max_chunks": 160},
}
DEFAULT_DEPTH = "standard"

# 受控关系词表：把同义关系收敛成有限集合，图结构才立得起来
RELATION_VOCAB = (
    "依赖", "使用", "包含", "属于", "用于", "产生", "影响", "替代",
    "支持", "实现", "训练", "评估", "存储", "检索", "生成", "优化",
    "对比", "组成", "前置", "相关",
)
RELATION_SYNONYMS = {
    "采用": "使用", "基于": "使用", "利用": "使用", "借助": "使用", "通过": "使用",
    "包括": "包含", "涵盖": "包含", "含有": "包含",
    "归属": "属于", "是": "属于",
    "应用于": "用于", "用以": "用于",
    "输出": "产生", "产出": "产生", "导致": "产生",
    "提升": "优化", "增强": "优化", "改善": "优化",
    "取代": "替代", "替换": "替代",
    "支撑": "支持", "提供": "支持",
    "落地": "实现", "完成": "实现",
    "微调": "训练", "训练于": "训练",
    "评测": "评估", "测试": "评估",
    "保存": "存储", "持久化": "存储",
    "召回": "检索", "搜索": "检索",
    "建立": "生成", "构造": "生成",
    "相比": "对比", "比较": "对比", "区别于": "对比",
    "构成": "组成",
    "前置条件": "前置", "前提": "前置",
    "关联": "相关", "联系": "相关", "涉及": "相关",
}
# 噪声切片：目录页、点线引导、过短内容
NOISE_MIN_CHARS = 80
NOISE_DOT_LEADER = "…"
NOISE_MAX_DIGIT_RATIO = 0.35

SUMMARY_SYSTEM = (
    "你是知识库的整理助手。请阅读给定文档片段，输出严格 JSON：\n"
    '{"summary": "200-300 字中文摘要", "category": "固定分类之一", "tags": ["3-6 个中文标签"]}\n'
    f"category 只能是：{'、'.join(CATEGORIES)}。"
    "摘要要说明文档讲什么、适合谁看、有哪些关键结论，不要写“本文介绍了”这类空话。"
    "tags 用名词短语，不要带标点，不要重复。只输出 JSON，不要解释。"
)

GRAPH_SYSTEM = (
    "你是知识图谱抽取器。从给定片段中抽取实体与关系，输出严格 JSON：\n"
    '{"entities": [{"name": "实体", "type": "技术/概念/工具/指标/组织", "chunk": 片段编号}],'
    ' "relations": [{"head": "实体A", "relation": "关系", "tail": "实体B", "chunk": 片段编号}]}\n'
    "只抽取片段中明确出现、且对理解内容有意义的实体与关系，不要罗列每个名词，不要补充外部知识。\n"
    f"relation 必须从下列词表中选一个：{'、'.join(RELATION_VOCAB)}；确实无法归入时用「相关」。\n"
    "选词要具体：能用「依赖」「包含」「属于」「用于」「产生」表达时不要用笼统的「使用」或「相关」。\n"
    "chunk 必须是片段编号（整数）。只输出 JSON，不要解释。"
)


def map_relation(raw: str) -> str:
    """把模型给出的关系收敛到受控词表，同义说法合并成一个。"""
    text = normalize_text(raw or "").strip()
    if not text:
        return ""
    if text in RELATION_VOCAB:
        return text
    if text in RELATION_SYNONYMS:
        return RELATION_SYNONYMS[text]
    for synonym, canonical in RELATION_SYNONYMS.items():
        if synonym in text:
            return canonical
    return "相关"


def batch_limits(batch_size: int) -> tuple[int, int]:
    """单批的实体/关系上限随该批片段数增长，避免小文档被过度抽取。"""
    entities = min(MAX_ENTITIES_PER_BATCH, 4 + 3 * batch_size)
    relations = min(MAX_RELATIONS_PER_BATCH, 3 + 2 * batch_size)
    return entities, relations


class EnrichmentPayload(BaseModel):
    summary: str = Field(min_length=10, max_length=1200)
    category: str = "其他"
    tags: list[str] = Field(default_factory=list)


class EntityPayload(BaseModel):
    name: str
    type: str = "概念"
    chunk: int = 1


class RelationPayload(BaseModel):
    head: str
    relation: str
    tail: str
    chunk: int = 1


class GraphPayload(BaseModel):
    entities: list[EntityPayload] = Field(default_factory=list)
    relations: list[RelationPayload] = Field(default_factory=list)


@dataclass
class EnrichmentResult:
    summary: str
    category: str
    tags: list[str]
    entities: int
    relations: int


def normalize_entity(name: str) -> str:
    """实体名规范化：去空白与标点、英文转小写，用于同文档内去重与匹配。"""
    text = normalize_text(name or "").strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[\"'“”‘’《》()（）\[\]【】,，.。:：;；!！?？]", "", text)
    return text.lower()


def _extract_json(raw: str) -> str:
    """去掉 ```json 围栏或前后多余文字，尽量取出对象。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _chat_json(system: str, user: str, schema: type[BaseModel]):
    """调用模型并解析 JSON，失败时带错误信息重试一次。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = llm.chat(
            messages,
            temperature=settings.llm_temperature_extract,
            max_tokens=settings.llm_max_tokens_extract,
            json_mode=True,
        )
        try:
            return schema.model_validate(json.loads(_extract_json(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("结构化输出解析失败（第 %d 次）：%s", attempt, str(exc)[:120])
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": "上面的输出不符合要求，请只输出合法 JSON，字段与取值必须符合约定。"}
            )
    raise AppError("enrichment_invalid_output", "模型输出不符合结构化要求", status_code=502) from last_error


def _pick_chunks(chunks: list[DocumentChunk], budget: int, limit: int) -> list[DocumentChunk]:
    """按开头、结尾、均匀采样挑选片段，控制输入长度。"""
    if not chunks:
        return []
    if len(chunks) <= limit:
        return chunks

    head = chunks[:2]
    tail = chunks[-1:]
    middle_pool = chunks[2:-1]
    need = max(0, limit - len(head) - len(tail))
    step = max(1, len(middle_pool) // need) if need else 1
    middle = middle_pool[::step][:need]

    picked: list[DocumentChunk] = []
    used = 0
    for chunk in head + middle + tail:
        if used + len(chunk.content) > budget and picked:
            continue
        picked.append(chunk)
        used += len(chunk.content)
    return picked


def _format_chunks(chunks: list[DocumentChunk]) -> str:
    return "\n\n".join(f"【{index}】{chunk.content}" for index, chunk in enumerate(chunks, start=1))


def summarize(document: Document, chunks: list[DocumentChunk]) -> tuple[str, str, list[str]]:
    picked = _pick_chunks(chunks, SUMMARY_BUDGET, limit=8)
    if not picked:
        raise AppError("empty_document", "没有可用于生成摘要的文本", status_code=422)

    user = f"文档名：{document.original_name}\n\n片段：\n{_format_chunks(picked)}"
    payload: EnrichmentPayload = _chat_json(SUMMARY_SYSTEM, user, EnrichmentPayload)

    category = payload.category.strip()
    if category not in CATEGORIES:
        # 模型给出集合外分类时归入「其他」，不制造新分类
        logger.info("分类不在固定集合内，归入其他：%s", category)
        category = "其他"

    tags: list[str] = []
    for tag in payload.tags:
        cleaned = normalize_text(tag).strip().replace("#", "")
        if cleaned and cleaned not in tags:
            tags.append(cleaned[:20])
        if len(tags) >= 6:
            break
    return normalize_text(payload.summary), category, tags


def resolve_depth(depth: str | None) -> dict:
    """把档位名解析成具体参数，未知值退回默认档。"""
    key = (depth or DEFAULT_DEPTH).strip().lower()
    return DEPTH_PRESETS.get(key, DEPTH_PRESETS[DEFAULT_DEPTH])


def graph_chunk_target(total: int, depth: str | None = None) -> int:
    """按文档切片数与档位决定抽取多少片：小文档全取，大文档按比例抽样并封顶。"""
    preset = resolve_depth(depth)
    if total <= GRAPH_MIN_CHUNKS:
        return total
    planned = max(GRAPH_MIN_CHUNKS, math.ceil(total * preset["ratio"]))
    return min(int(preset["max_chunks"]), planned)


def estimate_cost(total: int, depth: str | None = None) -> dict:
    """预估一次抽取的开销，供界面在按钮旁提示。"""
    preset = resolve_depth(depth)
    chunks = graph_chunk_target(total, depth)
    batches = max(1, math.ceil(chunks / GRAPH_BATCH_CHUNKS))
    return {
        "depth": (depth or DEFAULT_DEPTH),
        "label": preset["label"],
        "chunks": chunks,
        "batches": batches,
        # 摘要/标签 1 次 + 每批抽取 1 次，按每批约 3.5 秒估算
        "calls": 1 + batches,
        "seconds": int(2 + batches * 3.5),
        "ratio": preset["ratio"],
    }


def is_noise_chunk(content: str) -> bool:
    """过滤目录页、点线引导与过短内容，避免把目录当正文抽实体。"""
    text = (content or "").strip()
    if len(text) < NOISE_MIN_CHARS:
        return True
    dot_leaders = text.count(NOISE_DOT_LEADER) + text.count("....")
    if dot_leaders >= 3:
        return True
    digits = sum(1 for char in text if char.isdigit())
    return digits / max(1, len(text)) > NOISE_MAX_DIGIT_RATIO


def _drop_noise(chunks: list[DocumentChunk]) -> list[DocumentChunk]:
    kept = [chunk for chunk in chunks if not is_noise_chunk(chunk.content)]
    # 全被判为噪声时不再过滤，避免无内容可抽
    return kept or list(chunks)


def _spread_chunks(chunks: list[DocumentChunk], limit: int) -> list[DocumentChunk]:
    """在整个文档范围内均匀抽样，保证首尾与中段都有覆盖。"""
    if limit >= len(chunks):
        return list(chunks)
    step = len(chunks) / limit
    indexes = sorted({min(len(chunks) - 1, int(index * step)) for index in range(limit)})
    return [chunks[index] for index in indexes]


def _batch_chunks(picked: list[DocumentChunk]) -> list[list[DocumentChunk]]:
    """按字符数与片数切批，避免单次提示词过长。"""
    batches: list[list[DocumentChunk]] = []
    current: list[DocumentChunk] = []
    used = 0
    for chunk in picked:
        if current and (used + len(chunk.content) > GRAPH_BATCH_BUDGET or len(current) >= GRAPH_BATCH_CHUNKS):
            batches.append(current)
            current, used = [], 0
        current.append(chunk)
        used += len(chunk.content)
    if current:
        batches.append(current)
    return batches


def extract_graph(
    document: Document,
    chunks: list[DocumentChunk],
    depth: str | None = None,
) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str, str, str]], dict]:
    """按规模抽样 + 分批抽取实体关系，返回 (节点, 边, 采样统计)。"""
    if not chunks:
        return [], [], {"sampled": 0, "total": 0, "batches": 0, "depth": resolve_depth(depth)["label"]}

    usable = _drop_noise(chunks)
    target = graph_chunk_target(len(usable), depth)
    picked = _spread_chunks(usable, target)
    batches = _batch_chunks(picked)

    node_map: dict[str, tuple[str, str, str]] = {}
    candidates: list[tuple[str, str, str, str]] = []

    for batch in batches:
        local_ids = {index + 1: chunk.id for index, chunk in enumerate(batch)}
        max_entities, max_relations = batch_limits(len(batch))
        system = (
            f"{GRAPH_SYSTEM}\n本批共 {len(batch)} 个片段，"
            f"实体不超过 {max_entities} 个、关系不超过 {max_relations} 条。"
        )
        user = f"文档名：{document.original_name}\n\n片段：\n{_format_chunks(batch)}"
        try:
            payload: GraphPayload = _chat_json(system, user, GraphPayload)
        except AppError:
            # 单批失败不影响其他批次，最终以抽取到的内容为准
            logger.warning("图谱抽取跳过一批（%d 片）：%s", len(batch), document.original_name)
            continue

        for entity in payload.entities[:max_entities]:
            if len(node_map) >= MAX_ENTITIES_TOTAL:
                break
            key = normalize_entity(entity.name)
            if not key or key in node_map:
                continue
            chunk_id = local_ids.get(int(entity.chunk), batch[0].id)
            node_map[key] = (entity.name.strip()[:120], entity.type.strip()[:32] or "概念", chunk_id)

        for relation in payload.relations[:max_relations]:
            head_key = normalize_entity(relation.head)
            tail_key = normalize_entity(relation.tail)
            relation_name = map_relation(relation.relation)
            if not head_key or not tail_key or head_key == tail_key or not relation_name:
                continue
            chunk_id = local_ids.get(int(relation.chunk), batch[0].id)
            candidates.append((head_key, relation_name, tail_key, chunk_id))

    edges: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for head_key, relation_name, tail_key, chunk_id in candidates:
        if head_key not in node_map or tail_key not in node_map:
            continue
        signature = (head_key, relation_name, tail_key)
        if signature in seen:
            continue
        seen.add(signature)
        edges.append((head_key, relation_name, tail_key, chunk_id))
        if len(edges) >= MAX_RELATIONS_TOTAL:
            break

    logger.info(
        "图谱抽取：%s 覆盖 %d/%d 切片（%s 档，%d 批），实体 %d、关系 %d",
        document.original_name[:24],
        len(picked),
        len(chunks),
        resolve_depth(depth)["label"],
        len(batches),
        len(node_map),
        len(edges),
    )

    stats = {
        "sampled": len(picked),
        "total": len(chunks),
        "batches": len(batches),
        "depth": resolve_depth(depth)["label"],
    }
    nodes = [(key, name, entity_type, chunk_id) for key, (name, entity_type, chunk_id) in node_map.items()]
    return nodes, edges, stats


def _save_graph(document_id: str, nodes: list[tuple], edges: list[tuple]) -> tuple[int, int]:
    with session_scope() as session:
        session.execute(delete(GraphEdge).where(GraphEdge.document_id == document_id))
        session.execute(delete(GraphNode).where(GraphNode.document_id == document_id))

        id_by_key: dict[str, str] = {}
        for key, name, entity_type, chunk_id in nodes:
            node = GraphNode(
                id=str(uuid.uuid4()),
                document_id=document_id,
                normalized_name=key,
                name=name,
                entity_type=entity_type,
                source_chunk_id=chunk_id,
            )
            session.add(node)
            id_by_key[key] = node.id

        # 先落节点，再插边，避免外键约束失败
        session.flush()

        for head_key, relation, tail_key, chunk_id in edges:
            session.add(
                GraphEdge(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    source_node_id=id_by_key[head_key],
                    target_node_id=id_by_key[tail_key],
                    relation=relation,
                    source_chunk_id=chunk_id,
                )
            )
        return len(nodes), len(edges)


def enrich_document(
    document_id: str,
    *,
    with_graph: bool = True,
    depth: str | None = None,
) -> EnrichmentResult | None:
    """对单份文档执行知识组织，结果写回 documents / graph_* 表。"""
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            logger.warning("文档不存在，跳过知识组织：%s", document_id)
            return None
        chunks = list(
            session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id, DocumentChunk.is_parent.is_(False))
                .order_by(DocumentChunk.chunk_index)
            ).all()
        )
        document.enrichment_status = "processing"
        document.enrichment_error = None
        if not chunks:
            document.enrichment_status = "failed"
            document.enrichment_error = "文档没有切片，无法生成知识组织"
            return None

    try:
        summary, category, tags = summarize(document, chunks)
        node_count = edge_count = 0
        graph_stats = {"sampled": 0, "total": len(chunks), "batches": 0, "depth": resolve_depth(depth)["label"]}
        if with_graph:
            nodes, edges, graph_stats = extract_graph(document, chunks, depth=depth)
            node_count, edge_count = _save_graph(document_id, nodes, edges)

        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is not None:
                document.summary = summary
                document.category = category
                document.tags_json = tags
                document.enrichment_status = "done"
                document.enrichment_error = None
                document.graph_sampled_chunks = int(graph_stats.get("sampled", 0))
                document.graph_depth = str(depth or DEFAULT_DEPTH)
        logger.info(
            "知识组织完成：%s（分类=%s，标签=%d，实体=%d，关系=%d）",
            document_id[:8],
            category,
            len(tags),
            node_count,
            edge_count,
        )
        return EnrichmentResult(summary, category, tags, node_count, edge_count)
    except Exception as exc:
        message = exc.message if isinstance(exc, AppError) else f"{type(exc).__name__}: {exc}"
        logger.exception("知识组织失败：%s", document_id)
        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is not None:
                document.enrichment_status = "failed"
                document.enrichment_error = message[:500]
        return None


def start_enrich(document_id: str, *, with_graph: bool = True, depth: str | None = None):
    """后台执行知识组织，避免阻塞上传/接口。"""
    import threading

    thread = threading.Thread(
        target=enrich_document,
        kwargs={"document_id": document_id, "with_graph": with_graph, "depth": depth},
        daemon=True,
        name=f"enrich-{document_id[:8]}",
    )
    thread.start()
    return thread
