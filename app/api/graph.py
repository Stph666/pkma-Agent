"""知识图谱接口：按关联度排序、支持筛选，并返回来源切片。

排序与截断规则：
- 节点按关联边数（度数）降序，度数相同按名称，避免 limit 变成随机砍；
- 边只保留两端都可见的节点；同一对实体之间的多条关系合并成一条并标注种数；
- 度数为 0 的孤立实体默认隐藏，但单独报告数量，可由调用方显式要求展示。
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Document, DocumentChunk, GraphEdge, GraphNode
from app.schemas import GraphResponse

router = APIRouter(prefix="/api/graph", tags=["graph"])

SNIPPET_LENGTH = 120
CONTENT_LENGTH = 800
AUTO_ENTITY_LIMIT = 40


def _location(chunk: DocumentChunk | None) -> str:
    if chunk is None:
        return ""
    if chunk.page_start:
        return f"第 {chunk.page_start} 页"
    return f"片段 #{chunk.chunk_index}"


def _content(chunk: DocumentChunk | None) -> str:
    if chunk is None:
        return ""
    # 保留换行：公式与列表被压成一段会完全没法读
    return chunk.content[:6000]


def _first_sentence(text: str, limit: int = 60) -> str:
    body = (text or "").strip()
    for mark in ("。", "！", "？", "\n"):
        index = body.find(mark)
        if 0 <= index < limit:
            return body[: index + 1]
    return body[:limit]


def _snippet(chunk: DocumentChunk | None) -> str:
    if chunk is None:
        return ""
    text = chunk.content[:SNIPPET_LENGTH].replace("\n", " ")
    return text + ("…" if len(chunk.content) > SNIPPET_LENGTH else "")


@router.get("", response_model=GraphResponse, summary="图谱数据")
def get_graph(
    document_id: str | None = Query(default=None),
    q: str | None = Query(default=None, description="按实体名称模糊筛选"),
    types: list[str] | None = Query(default=None, description="实体类型多选"),
    min_degree: int = Query(default=0, ge=0, le=100, description="最小关联度"),
    include_isolated: bool = Query(default=False, description="是否包含度数为 0 的实体"),
    limit: int | None = Query(
        default=None,
        ge=-1,
        le=1000,
        description="最多显示多少个实体：0=自适应（≤40 全显示），-1 或空=不限",
    ),
    session: Session = Depends(get_db),
) -> GraphResponse:
    # 界面层与脚本会直接调用本函数，此时 FastAPI 的 Query 默认值仍是对象，
    # 这里统一归一化，避免出现 Query 与 int 混算的 TypeError。
    min_degree = min_degree if isinstance(min_degree, int) else 0
    include_isolated = include_isolated if isinstance(include_isolated, bool) else False
    limit = limit if isinstance(limit, int) else None

    node_stmt = select(GraphNode)
    if document_id:
        node_stmt = node_stmt.where(GraphNode.document_id == document_id)
    if q:
        node_stmt = node_stmt.where(GraphNode.name.contains(q.strip()))
    if types:
        node_stmt = node_stmt.where(GraphNode.entity_type.in_(types))
    all_nodes = list(session.scalars(node_stmt).all())

    edge_stmt = select(GraphEdge)
    if document_id:
        edge_stmt = edge_stmt.where(GraphEdge.document_id == document_id)
    all_edges = list(session.scalars(edge_stmt).all())

    # 度数只在当前筛选出的节点集合内计算，保证排序与画出的图一致
    node_ids = {node.id for node in all_nodes}
    degree: dict[str, int] = defaultdict(int)
    visible_edges: list[GraphEdge] = []
    for edge in all_edges:
        if edge.source_node_id in node_ids and edge.target_node_id in node_ids:
            degree[edge.source_node_id] += 1
            degree[edge.target_node_id] += 1
            visible_edges.append(edge)

    isolated = [node for node in all_nodes if degree.get(node.id, 0) == 0]
    floor = max(min_degree, 0 if include_isolated else 1)
    candidates = [node for node in all_nodes if degree.get(node.id, 0) >= floor]
    # 关联度高的排前面，同分按名称，保证截断结果稳定且有意义
    candidates.sort(key=lambda node: (-degree.get(node.id, 0), node.name))

    if limit is not None and limit < 0:
        limit = None
    elif limit == 0:
        # 自适应：总数不超过 40 就全显示，否则只给关联度最高的 40 个
        limit = None if len(candidates) <= AUTO_ENTITY_LIMIT else AUTO_ENTITY_LIMIT

    shown_nodes = candidates[:limit] if limit else candidates
    shown_ids = {node.id for node in shown_nodes}

    chunk_ids = {node.source_chunk_id for node in shown_nodes if node.source_chunk_id}
    chunk_ids |= {edge.source_chunk_id for edge in visible_edges if edge.source_chunk_id}
    chunks = {
        chunk.id: chunk
        for chunk in session.scalars(select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))).all()
    }
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_({node.document_id for node in shown_nodes}))
        ).all()
    }

    node_payload = [
        {
            "id": node.id,
            "name": node.name,
            "type": node.entity_type,
            "document_id": node.document_id,
            "chunk_id": node.source_chunk_id,
            "filename": documents[node.document_id].original_name if node.document_id in documents else "",
            "location": _location(chunks.get(node.source_chunk_id or "")),
            "page_start": (chunks.get(node.source_chunk_id or "").page_start
                           if chunks.get(node.source_chunk_id or "") else None),
            "snippet": _snippet(chunks.get(node.source_chunk_id or "")),
            "content": _content(chunks.get(node.source_chunk_id or "")),
            "degree": degree.get(node.id, 0),
        }
        for node in shown_nodes
    ]

    # 同一对实体的多条关系合并，避免图上出现大量重叠平行线
    grouped: dict[tuple[str, str], list[GraphEdge]] = defaultdict(list)
    for edge in visible_edges:
        if edge.source_node_id in shown_ids and edge.target_node_id in shown_ids:
            grouped[(edge.source_node_id, edge.target_node_id)].append(edge)

    names = {node.id: node.name for node in shown_nodes}
    edge_payload = []
    for (source, target), group in grouped.items():
        primary = group[0]
        relations = list(dict.fromkeys(edge.relation for edge in group))
        chunk = chunks.get(primary.source_chunk_id or "")
        edge_payload.append(
            {
                "source": source,
                "target": target,
                "relation": relations[0],
                "relation_count": len(relations),
                "relations": relations,
                "label": relations[0] if len(relations) == 1 else f"{len(relations)} 种关系",
                "document_id": primary.document_id,
                "location": _location(chunk),
                "snippet": _snippet(chunk),
                "content": _content(chunk),
                "source_name": names.get(source, ""),
                "target_name": names.get(target, ""),
            }
        )

    covered = len({node.source_chunk_id for node in shown_nodes if node.source_chunk_id})
    total_stmt = select(func.count()).select_from(DocumentChunk).where(DocumentChunk.is_parent.is_(False))
    if document_id:
        total_stmt = total_stmt.where(DocumentChunk.document_id == document_id)
    total_chunks = int(session.scalar(total_stmt) or 0)

    # 采样片数与档位：单文档直接读，全部文档按累加，如实反映抽取范围
    if document_id:
        document_row = session.get(Document, document_id)
        sampled = int(getattr(document_row, "graph_sampled_chunks", 0) or 0)
        depth_key = (getattr(document_row, "graph_depth", "") or "").strip()
    else:
        sampled = int(
            session.scalar(
                select(func.coalesce(func.sum(Document.graph_sampled_chunks), 0)).where(
                    Document.graph_sampled_chunks > 0
                )
            )
            or 0
        )
        depth_key = ""

    from app.services import enrichment as enrichment_service

    depth_label = enrichment_service.resolve_depth(depth_key)["label"] if depth_key else "混合"

    return GraphResponse(
        nodes=node_payload,
        edges=edge_payload,
        total_entities=len(all_nodes),
        shown_entities=len(node_payload),
        total_relations=len(visible_edges),
        shown_relations=len(edge_payload),
        hidden_isolated=len(isolated),
        truncated=len(node_payload) < len(candidates),
        degree_histogram={
            "high": sum(1 for node in all_nodes if degree.get(node.id, 0) >= 3),
            "low": sum(1 for node in all_nodes if 1 <= degree.get(node.id, 0) <= 2),
            "isolated": len(isolated),
        },
        covered_chunks=covered,
        total_chunks=total_chunks,
        sampled_chunks=sampled,
        depth=depth_key,
        depth_label=depth_label,
    )


@router.get("/html", summary="图谱 iframe（供前端直接嵌入）")
def get_graph_html(
    document_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    limit: int | None = Query(default=None),
    include_isolated: bool = Query(default=False),
    session: Session = Depends(get_db),
) -> dict:
    """复用 Gradio 版的图谱模板，返回可直接放进 iframe 的整页 HTML。"""
    from app.ui.graph_view import build_graph_html

    payload = get_graph(
        document_id=document_id,
        q=q,
        types=types,
        min_degree=0,
        include_isolated=include_isolated,
        limit=limit,
        session=session,
    )
    html = build_graph_html(
        [node.model_dump() for node in payload.nodes],
        [edge.model_dump() for edge in payload.edges],
    )
    return {
        "html": html,
        "shown_entities": payload.shown_entities,
        "total_entities": payload.total_entities,
        "shown_relations": payload.shown_relations,
        "total_relations": payload.total_relations,
        "truncated": payload.truncated,
    }


@router.get("/explore", summary="关系图谱数据（含共现/同名边）")
def get_explore(
    document_id: str | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    limit: int = Query(default=240, ge=20, le=600),
    include_cooccur: bool = Query(default=True),
    include_alias: bool = Query(default=True),
    session: Session = Depends(get_db),
) -> dict:
    from app.ui.graph_explore import build_payload

    return build_payload(
        session,
        document_id=document_id,
        types=types,
        limit=limit,
        include_cooccur=include_cooccur,
        include_alias=include_alias,
    )


@router.get("/explore/html", summary="关系图谱（Cytoscape 渲染）")
def get_explore_html(
    document_id: str | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    limit: int = Query(default=240, ge=20, le=600),
    include_cooccur: bool = Query(default=True),
    include_alias: bool = Query(default=True),
    session: Session = Depends(get_db),
) -> dict:
    from app.ui.graph_explore import build_payload, to_html

    payload = build_payload(
        session,
        document_id=document_id,
        types=types,
        limit=limit,
        include_cooccur=include_cooccur,
        include_alias=include_alias,
    )
    return {"html": to_html(payload), "stats": payload["stats"]}


@router.get("/mindmap", summary="知识树（思维导图）")
def get_mindmap(
    document_id: str | None = Query(default=None),
    limit: int = Query(default=300, ge=20, le=800),
    session: Session = Depends(get_db),
) -> dict:
    """按「文档 → 章节 → 实体」返回层级树，并给出可直接嵌入 iframe 的 HTML。"""
    from app.ui.mindmap_view import build_knowledge_tree, to_html

    tree = build_knowledge_tree(session, document_id=document_id, limit=limit)
    entities = sum(
        len(chapter.get("children", []))
        for doc in tree.get("children", [])
        for chapter in doc.get("children", [])
    )
    return {"html": to_html(tree), "entities": entities, "tree": tree}


@router.get("/stats", summary="图谱规模统计")
def graph_stats(document_id: str | None = Query(default=None), session: Session = Depends(get_db)) -> dict:
    node_stmt = select(GraphNode)
    edge_stmt = select(GraphEdge)
    if document_id:
        node_stmt = node_stmt.where(GraphNode.document_id == document_id)
        edge_stmt = edge_stmt.where(GraphEdge.document_id == document_id)
    return {
        "nodes": int(session.scalar(select(func.count()).select_from(node_stmt.subquery())) or 0),
        "edges": int(session.scalar(select(func.count()).select_from(edge_stmt.subquery())) or 0),
    }


@router.get("/extract-plan", summary="预览不同抽取档位的开销")
def extract_plan(document_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    from app.services import enrichment

    total = int(
        session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id, DocumentChunk.is_parent.is_(False))
        )
        or 0
    )
    return {
        "chunks": total,
        "plans": [enrichment.estimate_cost(total, key) for key in enrichment.DEPTH_PRESETS],
    }
