"""检索：向量召回 + 相似度过滤 + 片段去重。

图谱关联扩展在阶段 D 接入，这里预留 `expand_with_graph` 钩子，
默认不做任何扩展，保持“向量检索为唯一基线”的可对照性。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from app.config import settings
from app.integrations import embeddings, vector_store
from app.services import bm25

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    page_start: int | None
    page_end: int | None
    content: str
    score: float
    origin: str = "vector"
    # 命中子片的原文：父块给模型看，子片只用来定位「哪一句最相关」
    highlight: str = ""

    @property
    def location(self) -> str:
        if self.origin == "graph":
            return "知识图谱"
        if self.page_start and self.page_end and self.page_start != self.page_end:
            return f"第 {self.page_start}–{self.page_end} 页"
        if self.page_start:
            return f"第 {self.page_start} 页"
        return f"片段 #{self.chunk_index}"


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    top_score: float = 0.0
    threshold: float = 0.0
    used_graph: bool = False
    # high：证据充分；partial：勉强相关，只能部分回答；none：无依据
    tier: str = "none"

    @property
    def has_evidence(self) -> bool:
        return bool(self.chunks)


VAGUE_PATTERNS = ("含义", "意思", "是什么", "什么是", "解释", "介绍", "说明一下", "讲讲")


def rewrite_query(question: str, history: list[dict] | None = None) -> str:
    """把口语化问题改写成更适合向量检索的查询。

    两类处理：
    1. 定义类（什么是 X / X 是什么 / 解释 X）→ 补「X 的定义」，实测能把定义段从十几名拉到前二；
    2. 指代不明（解释一下含义）→ 有历史时补上上一轮主题。
    """
    text = (question or "").strip()
    if not text:
        return text

    entity = extract_entity(text)
    definitional = any(hint in text for hint in DEFINITION_HINTS)

    if definitional and len(entity) >= 2:
        return f"{text} {entity} 的定义"

    if definitional and history:
        previous = next(
            (item.get("content", "") for item in reversed(history) if item.get("role") == "user"),
            "",
        ).strip()
        if previous and previous != text:
            return f"{previous} {text}"

    return text


DEFINITION_PATTERNS = ("是指", "指的是", "定义为", "由role", "包括")
DEFINITION_HINTS = ("含义", "是什么", "什么是", "定义", "解释", "指什么", "什么意思")
QUESTION_NOISE = (
    "什么是", "是什么", "的含义", "含义", "的意思", "什么意思", "请解释", "解释一下",
    "解释", "介绍一下", "介绍", "一下", "请问", "？", "?", "。", "，",
)


def extract_entity(question: str) -> str:
    """从「什么是 Agent」里取出待定义实体「Agent」。"""
    text = (question or "").strip()
    for noise in QUESTION_NOISE:
        text = text.replace(noise, " ")
    return text.strip().split(" ")[0] if text.strip() else ""




def _expand_to_parents(candidates: list[vector_store.VectorHit]) -> list[RetrievedChunk]:
    """子片按 parent_id 聚合：同一父块只保留最高分的子片，返回父块正文 + 子片高亮。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import DocumentChunk

    order: list[str] = []
    best: dict[str, vector_store.VectorHit] = {}
    for hit in candidates:
        parent_id = (hit.metadata or {}).get("parent_id") or hit.id
        if parent_id not in best:
            order.append(parent_id)
            best[parent_id] = hit
        elif hit.score > best[parent_id].score:
            best[parent_id] = hit

    parent_ids = [pid for pid in order if pid in best and pid != best[pid].id]
    rows: dict[str, DocumentChunk] = {}
    if parent_ids:
        with session_scope() as session:
            rows = {
                row.id: row
                for row in session.scalars(
                    select(DocumentChunk).where(DocumentChunk.id.in_(parent_ids))
                ).all()
            }

    expanded: list[RetrievedChunk] = []
    for parent_id in order:
        hit = best[parent_id]
        row = rows.get(parent_id)
        if row is None:
            expanded.append(_to_chunk(hit))
            continue
        meta = hit.metadata or {}
        expanded.append(
            RetrievedChunk(
                chunk_id=row.id,
                document_id=row.document_id,
                filename=str(meta.get("filename", "未知文档")),
                chunk_index=row.chunk_index,
                page_start=row.page_start,
                page_end=row.page_end,
                content=row.content,
                score=hit.score,
                origin="parent",
                highlight=hit.document,
            )
        )
    return expanded



def _ensure_compare_coverage(
    kept: list[RetrievedChunk],
    question: str,
    document_ids: list[str] | None,
    limit: int,
) -> list[RetrievedChunk]:
    """对比题：给每个实体各留一个坑位，避免一侧被另一侧的高分淹没。"""
    from app.services.query_understanding import _compare_queries

    parts = _compare_queries(question)
    if not parts:
        return kept

    existing = {chunk.chunk_id for chunk in kept}
    additions: list[RetrievedChunk] = []
    for part in parts:
        vector = embeddings.encode_query(part)
        hits = vector_store.query(vector, top_k=8, document_ids=document_ids or None)
        if not hits:
            continue
        best = max(hits, key=lambda hit: hit.score)
        expanded = _expand_to_parents([best])
        if not expanded:
            continue
        chunk = expanded[0]
        if chunk.chunk_id in existing:
            continue
        # 这一侧至少要有一条真实文本包含该实体，否则不塞
        if part.lower() not in chunk.content.lower() and part.lower() not in chunk.highlight.lower():
            fallback = _entity_chunk(part, document_ids)
            if fallback is None or fallback.chunk_id in existing:
                continue
            chunk = fallback
            chunk.origin = "compare"
        existing.add(chunk.chunk_id)
        additions.append(chunk)

    return (additions + kept)[:limit] if additions else kept



RELATION_HINTS = ("关系", "关联", "相关", "联系", "区别", "差别", "对比")



def _entity_chunk(part: str, document_ids: list[str] | None) -> RetrievedChunk | None:
    """兜底：直接按实体名在库里找一段包含它的父块。

    子片切得细时，向量 top-1 可能是「提到这个词但主题不同」的片段，
    对比题就会缺一侧；这里退化为一次 LIKE，保证两侧都有内容。
    """
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Document, DocumentChunk

    with session_scope() as session:
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.content.like(f"%{part}%"), DocumentChunk.is_parent.is_(True))
            .limit(5)
        )
        if document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
        # MySQL 的 LIKE 默认不区分大小写，会命中 makto 之类；这里再按词本身过滤一遍
        rows = [
            row
            for row in session.scalars(stmt).all()
            if part.lower() in (row.content or "").lower()
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: (row.content or "").count(part), reverse=True)
        row = rows[0]
        name = session.get(Document, row.document_id)
        return RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            filename=name.original_name if name else "未知文档",
            chunk_index=row.chunk_index,
            page_start=row.page_start,
            page_end=row.page_end,
            content=row.content,
            score=0.0,
            origin="parent",
        )


def _graph_context(question: str, document_ids: list[str] | None) -> list[RetrievedChunk]:
    """关系类问题：把图谱里该实体的邻居拼成一条上下文。

    文本片段擅长讲「是什么」，讲不了「和谁有什么关系」；这部分知识本来就在图谱里。
    """
    if not any(hint in question for hint in RELATION_HINTS):
        return []

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Document, GraphEdge, GraphNode

    # 优先取引号里的词；「X 和其他概念的关系」这类问法里，extract_entity 会把整串都带上
    quoted = re.findall(r"[「“\"]([^」”\"]{2,20})[」”\"]", question)
    entity = quoted[0] if quoted else extract_entity(question)
    entity = re.split(r"(?:和|与|跟|及|以及)", entity)[0].strip()
    if not entity or len(entity) < 2:
        return []

    with session_scope() as session:
        stmt = select(GraphNode).where(GraphNode.name.like(f"%{entity}%"))
        if document_ids:
            stmt = stmt.where(GraphNode.document_id.in_(document_ids))
        nodes = list(session.scalars(stmt.limit(5)).all())
        if not nodes:
            return []

        node_ids = {node.id for node in nodes}
        names = {node.id: node.name for node in nodes}
        edges = [
            edge
            for edge in session.scalars(select(GraphEdge)).all()
            if edge.source_node_id in node_ids or edge.target_node_id in node_ids
        ]
        if not edges:
            return []

        extra_ids = {edge.source_node_id for edge in edges} | {edge.target_node_id for edge in edges}
        extra = {
            node.id: node.name
            for node in session.scalars(select(GraphNode).where(GraphNode.id.in_(extra_ids))).all()
        }
        doc_names = {
            doc.id: doc.original_name for doc in session.scalars(select(Document)).all()
        }

        lines: list[str] = []
        for edge in edges[:14]:
            head = extra.get(edge.source_node_id, names.get(edge.source_node_id, "?"))
            tail = extra.get(edge.target_node_id, names.get(edge.target_node_id, "?"))
            lines.append(f"{head} —{edge.relation}→ {tail}")
        if not lines:
            return []

        first = nodes[0]
        return [
            RetrievedChunk(
                chunk_id=f"graph-{first.id}",
                document_id=first.document_id,
                filename=doc_names.get(first.document_id, "知识图谱"),
                chunk_index=0,
                page_start=None,
                page_end=None,
                content="【知识图谱中的关系】\n" + "\n".join(lines),
                score=0.0,
                origin="graph",
            )
        ]


def _definition_snippet(entity: str, document_ids: list[str] | None, limit: int = 2) -> list[RetrievedChunk]:
    """定向抽取「实体：由/是/指…」这种定义句。

    token 切片会把定义句并进讲别的内容的大切片，向量相似度被稀释、排不进前几。
    这里按句式直接截取句子，作为补充候选放在最前面。
    """
    if not entity or len(entity) < 2:
        return []

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Document, DocumentChunk

    pattern = re.compile(
        rf"{re.escape(entity)}\s*(?:[：:]\s*(?:由|是|指|包括|即)|"
        rf"\b(?:is|are|refers to|means|denotes)\b)\s*(.{{10,220}})",
        re.IGNORECASE | re.S,
    )
    with session_scope() as session:
        # 先用「实体 + 冒号」精确捞，再退回模糊匹配；否则前 300 条里可能根本没有定义句
        rows: list[DocumentChunk] = []
        for needle in (f"%{entity}：%", f"%{entity}:%"):
            stmt = select(DocumentChunk).where(DocumentChunk.content.like(needle)).limit(120)
            if document_ids:
                stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
            rows.extend(session.scalars(stmt).all())
            if len(rows) >= 120:
                break
        if not rows:
            stmt = select(DocumentChunk).where(DocumentChunk.content.like(f"%{entity}%")).limit(300)
            if document_ids:
                stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
            rows = list(session.scalars(stmt).all())
        picked: list[tuple[DocumentChunk, str]] = []
        for row in rows:
            body = row.content
            if body.count("…") >= 2 or len(re.findall(r"\.{4,}", body)) >= 1:
                continue
            match = pattern.search(body)
            if not match:
                continue
            start = max(0, match.start() - 20)
            snippet = body[start : match.end()].strip().replace("\n", " ")
            if len(snippet) < 30:
                continue
            picked.append((row, snippet[:260]))
            if len(picked) >= limit:
                break

        # 切片有重叠，同一句话可能在相邻切片里重复出现，按内容去重
        deduped: list[tuple[DocumentChunk, str]] = []
        seen_text: set[str] = set()
        for row, snippet in picked:
            key = snippet[:80]
            if key in seen_text:
                continue
            seen_text.add(key)
            deduped.append((row, snippet))
        picked = deduped
        if not picked:
            return []
        names = {
            doc.id: doc.original_name
            for doc in session.scalars(
                select(Document).where(Document.id.in_({row.document_id for row, _ in picked}))
            ).all()
        }
        return [
            RetrievedChunk(
                chunk_id=f"{row.id}-def",
                document_id=row.document_id,
                filename=names.get(row.document_id, "未知文档"),
                chunk_index=row.chunk_index,
                page_start=row.page_start,
                page_end=row.page_end,
                content=snippet,
                score=0.0,
                origin="definition",
            )
            for row, snippet in picked
        ]


def _definition_bonus(question: str, document: str) -> float:
    """定义类问题优先取「实体：由/是/指」这种以实体开头的句式。

    BGE 对「X 是什么」的召回偏弱，定义句常排在十几名；
    但「多Agent 系统……」也含实体和冒号，所以必须锚定行首才能区分。
    """
    if not any(hint in question for hint in DEFINITION_HINTS):
        return 0.0

    entity = extract_entity(question)
    if entity:
        anchored = re.compile(rf"^(?:[\s•·\-*]|\d+[\.、])*{re.escape(entity)}\s*[：:]", re.IGNORECASE)
        if anchored.search(document[:40]):
            return 0.35

    return 0.08 if any(pattern in document[:120] for pattern in DEFINITION_PATTERNS) else 0.0


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _to_chunk(hit: vector_store.VectorHit) -> RetrievedChunk:
    meta = hit.metadata or {}
    return RetrievedChunk(
        chunk_id=hit.id,
        document_id=str(meta.get("document_id", "")),
        filename=str(meta.get("filename", "未知文档")),
        chunk_index=int(meta.get("chunk_index", 0) or 0),
        page_start=meta.get("page_start"),
        page_end=meta.get("page_end"),
        content=hit.document,
        score=hit.score,
    )



HYDE_SYSTEM = (
    "你是检索助手。针对用户的问题，写一段 120 字左右的假设性答案，"
    "用陈述句、尽量使用该领域可能出现的术语，不要解释、不要分点、不要回答用户。"
)


def _hyde_text(question: str) -> str:
    """让模型先写一段“假想答案”，用它的语义去检索，弥补短问句与正文的用词差异。"""
    from app.integrations import llm

    try:
        text = llm.chat(
            [
                {"role": "system", "content": HYDE_SYSTEM},
                {"role": "user", "content": question},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        return (text or "").strip().replace("\n", " ")[:200]
    except Exception:
        return ""


def _multi_query_pool(question: str, pool_size: int, document_ids: list[str] | None):
    """按需多路召回：原问句 + HyDE 文本，按 chunk_id 合并取最高分。"""
    queries = [question]
    if settings.query_understanding_enabled:
        from app.services import query_understanding

        queries.extend(query_understanding.extra_queries(question))
    if settings.hyde_enabled:
        fake = _hyde_text(question)
        if fake:
            queries.append(fake)

    # 主路（原问句）结果保序在前；扩展路只补尾部，避免「knowledge graph」把图论章节顶上来
    primary = vector_store.query(
        embeddings.encode_query(question), top_k=pool_size, document_ids=document_ids or None
    )
    # 0.56 = 主路「够自信」的经验阈值。解决的是：中文问「知识图谱的三元组」时，
    # LLM 生成的英文扩展词 "knowledge graph" 会把 Cormen 图论章节按 0.7+ 顶到第一，
    # 正确片段（0.61）反被挤掉。高于此值时只走主路；低于时才用扩展词救召回。
    # 改这个数会同时影响「中文问英文书」的召回（那类主路通常 0.45–0.50，必须保留扩展）。
    if primary and primary[0].score >= 0.56:
        return primary[:pool_size]

    merged: dict[str, vector_store.VectorHit] = {hit.id: hit for hit in primary}
    extra: list[vector_store.VectorHit] = []
    for query in queries[1:]:
        vector = embeddings.encode_query(query)
        for hit in vector_store.query(vector, top_k=pool_size, document_ids=document_ids or None):
            if hit.id in merged:
                continue
            merged[hit.id] = hit
            extra.append(hit)
    # 主路信心不足时才靠扩展词救召回，这时按分数合并
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:pool_size]


def search(
    question: str,
    *,
    document_ids: list[str] | None = None,
    top_k: int | None = None,
    context_k: int | None = None,
    threshold: float | None = None,
    use_graph: bool | None = None,
) -> RetrievalResult:
    """向量召回并按阈值过滤，可选做图谱一跳扩展。"""
    question = (question or "").strip()
    if not question:
        return RetrievalResult(threshold=threshold if threshold is not None else settings.relevance_threshold)

    pool_size = max(settings.retrieval_pool, top_k or settings.retrieval_top_k)
    context_limit = context_k or settings.context_top_k
    score_floor = settings.relevance_threshold if threshold is None else threshold
    partial_floor = settings.partial_floor
    # bge-m3 的余弦分布被压缩（相关 0.58–0.64，噪声 0.49–0.57），
    # 沿用 small-zh 时代的 0.45/0.32 会把「红烧肉怎么做」判成有依据。
    if threshold is None and "m3" in (settings.embedding_model or "").lower():
        score_floor = max(score_floor, 0.56)
        partial_floor = max(partial_floor, 0.50)

    pool = _multi_query_pool(question, pool_size, document_ids)

    # 1) 先用向量阈值筛一遍，区分「证据充分 / 勉强相关 / 无依据」
    qualified = [hit for hit in pool if hit.score >= score_floor]
    top_score = max((hit.score for hit in pool), default=0.0)

    if qualified:
        tier = "high"
        # 阈值只用来判断“证据是否充分”，候选仍取整个池：定义段这类句子向量分可能偏低，
        # 但句式上正是答案，先过滤掉就再也救不回来了。
        candidates = [hit for hit in pool if hit.score >= partial_floor]
    elif pool and top_score >= partial_floor:
        tier = "partial"
        candidates = pool[: max(5, context_limit)]
    else:
        tier = "none"
        candidates = []

    # 2) 候选集内做关键词重排：向量分 + BM25 融合，避免“定义段”被相似片段挤掉
    if candidates:
        if settings.hybrid_enabled:
            lexical = bm25.score(question, [hit.document for hit in candidates])
            fused = bm25.fuse(
                [hit.score for hit in candidates],
                lexical,
                settings.hybrid_keyword_weight,
            )
        else:
            fused = bm25.normalize([hit.score for hit in candidates])

        fused = [
            value + _definition_bonus(question, hit.document)
            for value, hit in zip(fused, candidates)
        ]
        order = sorted(range(len(candidates)), key=lambda index: fused[index], reverse=True)
        candidates = [candidates[index] for index in order]

        if settings.rerank_enabled:
            from app.integrations import reranker

            candidates = reranker.rerank(question, candidates, top_k=len(candidates))

            # 换 bge-m3 后向量分整体被压缩：无关问题也能到 0.49–0.57，
            # 单纯用 0.45 判「有依据」会全部误判。交叉编码器的分数区分度更好
            # （相关 0.9+，无关 0.0–0.2），所以有重排时以它为准。
            top_rerank = getattr(candidates[0], "rerank_score", None) if candidates else None
            if top_rerank is not None:
                if top_rerank >= settings.rerank_high:
                    tier = "high"
                elif top_rerank >= settings.rerank_partial:
                    tier = "partial"
                else:
                    tier = "none"
                    candidates = []
            elif top_score < settings.rerank_missing_floor:
                # 模型不可用时不能沿用向量阈值（bge-m3 下噪声分能到 0.57）
                tier = "partial" if top_score >= partial_floor else "none"
                candidates = candidates[: max(3, context_limit)] if tier == "partial" else []

    kept: list[RetrievedChunk] = []
    if settings.parent_child_enabled and candidates:
        kept = _expand_to_parents(candidates)[:context_limit]
    else:
        seen: set[str] = set()
        for hit in candidates:
            if hit.id in seen:
                continue
            seen.add(hit.id)
            kept.append(_to_chunk(hit))
            if len(kept) >= context_limit:
                break

    # 定义类问题补一条定向命中的定义句，放在最前面
    if kept and any(hint in question for hint in DEFINITION_HINTS):
        extras = _definition_snippet(extract_entity(question), document_ids)
        if extras:
            existing = {chunk.chunk_id for chunk in kept}
            extras = [chunk for chunk in extras if chunk.chunk_id not in existing]
            for chunk in extras:
                chunk.score = round(top_score, 4)
            kept = extras + kept
            kept = kept[: max(context_limit, len(extras) + 1)]

    graph_extra = _graph_context(question, document_ids)
    if graph_extra:
        graph_extra[0].score = round(max(0.0, top_score - 0.02), 4)
        kept = (graph_extra + kept)[: context_limit]

    # 兜底放最后：确保对比题两侧都在，不会被图谱片段挤掉
    kept = _ensure_compare_coverage(kept, question, document_ids, max(context_limit, 7))

    result = RetrievalResult(chunks=kept, top_score=top_score, threshold=score_floor, tier=tier)

    expand = settings.graph_expansion_enabled if use_graph is None else use_graph
    if expand and kept:
        result = expand_with_graph(question, result, document_ids=document_ids)

    logger.info(
        "检索：问题「%s」池 %d 条，达标 %d 条，档位 %s，图谱补充 %d 条（阈值 %.2f，最高分 %.2f）",
        question[:30],
        len(pool),
        len(qualified),
        tier,
        sum(1 for chunk in result.chunks if chunk.origin == "graph"),
        score_floor,
        top_score,
    )
    return result


# 常见别名：让「RAG」「检索增强生成」这类同义说法都能命中同一实体
ENTITY_ALIASES = {
    "rag": ("检索增强生成", "检索增强"),
    "llm": ("大语言模型", "大模型"),
    "kg": ("知识图谱",),
    "向量库": ("向量数据库",),
    "向量检索": ("语义检索",),
    "切片": ("分块", "chunk"),
    "召回": ("检索",),
    "微调": ("fine-tune", "微调训练"),
}
MAX_MATCHED_ENTITIES = 2
MIN_ENTITY_NAME_LEN = 2


def _match_entities(question: str, nodes: list) -> list:
    """在问题中匹配实体：别名扩展 + 长名优先，最多返回 2 个。"""
    text = (question or "").lower()
    expanded = text
    for alias, targets in ENTITY_ALIASES.items():
        if alias in text:
            expanded += " " + " ".join(targets)
            continue
        # 反向：问题里用了全称，别名也算命中
        if any(target.lower() in text for target in targets):
            expanded += " " + alias

    matched = [
        node
        for node in nodes
        if node.normalized_name
        and len(node.normalized_name) >= MIN_ENTITY_NAME_LEN
        and node.normalized_name in expanded
    ]
    # 长名优先：避免「向量」把「向量数据库」的命中挤掉
    matched.sort(key=lambda node: len(node.normalized_name), reverse=True)

    # 去掉被更长命中项包含的短名（「向量」⊂「向量数据库」），避免重复补充同一段内容
    kept: list = []
    for node in matched:
        if any(node.normalized_name in longer.normalized_name for longer in kept):
            continue
        kept.append(node)
        if len(kept) >= MAX_MATCHED_ENTITIES:
            break
    return kept


def expand_with_graph(
    question: str,
    result: RetrievalResult,
    *,
    document_ids: list[str] | None = None,
    max_extra: int = 2,
) -> RetrievalResult:
    """问题命中图谱实体时，补充其一跳邻居的来源切片。

    只在实体名明确出现在问题中时触发；补充数量受限，保持“向量为主、图谱为辅”，
    也便于阶段 H 做「仅向量」与「向量 + 图谱」的对照实验。
    """
    from sqlalchemy import or_, select

    from app.db import session_scope
    from app.models import Document, DocumentChunk, GraphEdge, GraphNode

    if not question.strip():
        return result

    normalized_question = question.lower()
    with session_scope() as session:
        node_stmt = select(GraphNode).limit(2000)
        if document_ids:
            node_stmt = node_stmt.where(GraphNode.document_id.in_(document_ids))
        nodes = list(session.scalars(node_stmt).all())
        matched = _match_entities(normalized_question, nodes)
        if not matched:
            return result

        matched_ids = {node.id for node in matched}
        edges = list(
            session.scalars(
                select(GraphEdge).where(
                    or_(GraphEdge.source_node_id.in_(matched_ids), GraphEdge.target_node_id.in_(matched_ids))
                )
            ).all()
        )
        neighbor_ids = set()
        for edge in edges:
            neighbor_ids.add(edge.source_node_id)
            neighbor_ids.add(edge.target_node_id)
        neighbor_ids -= matched_ids
        if not neighbor_ids:
            return result

        neighbors = list(session.scalars(select(GraphNode).where(GraphNode.id.in_(neighbor_ids))).all())
        chunk_ids = {node.source_chunk_id for node in neighbors if node.source_chunk_id}
        chunk_ids |= {edge.source_chunk_id for edge in edges if edge.source_chunk_id}
        existing = {chunk.chunk_id for chunk in result.chunks}
        chunk_ids -= existing
        if not chunk_ids:
            return result

        chunks = list(session.scalars(select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))).all())
        filenames = {
            document.id: document.original_name
            for document in session.scalars(
                select(Document).where(Document.id.in_({chunk.document_id for chunk in chunks}))
            ).all()
        }

    added: list[RetrievedChunk] = []
    for chunk in sorted(chunks, key=lambda item: item.chunk_index):
        if len(added) >= max_extra:
            break
        added.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                filename=filenames.get(chunk.document_id, "未知文档"),
                chunk_index=chunk.chunk_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                content=chunk.content,
                score=0.0,
                origin="graph",
            )
        )

    if not added:
        return result

    logger.info("图谱扩展：命中实体 %s，补充 %d 条切片", [node.name for node in matched][:3], len(added))
    result.chunks = result.chunks + added
    result.used_graph = True
    return result


def search_raw(
    question: str,
    *,
    document_ids: list[str] | None = None,
    top_k: int = 10,
) -> list[RetrievedChunk]:
    """不做阈值过滤的召回，用于评估集计算 Hit@k 与对照实验。"""
    vector = embeddings.encode_query(question)
    hits = vector_store.query(vector, top_k=top_k, document_ids=document_ids or None)
    return [_to_chunk(hit) for hit in hits]

