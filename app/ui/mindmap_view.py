"""知识树：把平铺的实体按「文档 → 章节 → 实体」组织成层级，供思维导图渲染。

力导向图只能表达「有关系」，层级图才能表达「属于」。这里不引入额外抽取，
直接用实体所在切片的首行标题 / 页码推导章节，零新增 LLM 成本。
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, GraphEdge, GraphNode

CHAPTER_PATTERN = re.compile(r"^(第[一二三四五六七八九十百零两\d]+[章节篇部]|\d+(?:\.\d+){1,3})\s*")
MAX_ENTITY_PER_CHAPTER = 40


def _chapter_anchor(chunk: DocumentChunk) -> str | None:
    """切片开头若像章节标题，就当作一个锚点。"""
    for line in chunk.content.splitlines()[:2]:
        text = line.strip()
        if 2 <= len(text) <= 40 and CHAPTER_PATTERN.match(text):
            return text[:32]
    return None


def _anchors_for(session: Session, document_id: str) -> list[tuple[int, str]]:
    """文档内所有章节锚点，按切片顺序排列。"""
    rows = session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    anchors: list[tuple[int, str]] = []
    for chunk in rows:
        label = _chapter_anchor(chunk)
        if label:
            anchors.append((chunk.chunk_index, label))
    return anchors


def _chapter_label(chunk: DocumentChunk | None, anchors: list[tuple[int, str]] | None = None) -> tuple[str, int]:
    """返回（章节名, 排序键）。标题优先，退化成页码区间。"""
    if chunk is None:
        return "未关联片段", 10**6

    own = _chapter_anchor(chunk)
    if own:
        return own, (chunk.page_start or 0) * 1000

    # 实体所在切片通常不是标题行，归到它前面最近的一个章节锚点
    if anchors:
        label = None
        for index, text in anchors:
            if index <= chunk.chunk_index:
                label = text
            else:
                break
        if label:
            return label, (chunk.page_start or 0) * 1000

    if chunk.page_start:
        end = chunk.page_end or chunk.page_start
        return f"第 {chunk.page_start}–{end} 页", chunk.page_start * 1000
    return "未分章", 10**6


def _short_doc_name(name: str) -> str:
    """缩短文档名：去掉扩展名与 LLM-Master- 前缀，保留最有辨识度的部分。"""
    base = name.rsplit(".", 1)[0]
    base = base.replace("LLM-Master-", "").replace(" Introduction to Algorithms", " 算法导论")
    return base if len(base) <= 20 else base[:19] + "…"


def build_knowledge_tree(
    session: Session,
    document_id: str | None = None,
    limit: int = 400,
) -> dict:
    """组装知识树；超出上限时优先保留连接度高的实体。"""
    stmt = select(GraphNode)
    if document_id:
        stmt = stmt.where(GraphNode.document_id == document_id)
    nodes = list(session.scalars(stmt).all())

    degree: dict[str, int] = {}
    for edge in session.scalars(select(GraphEdge)).all():
        degree[edge.source_node_id] = degree.get(edge.source_node_id, 0) + 1
        degree[edge.target_node_id] = degree.get(edge.target_node_id, 0) + 1

    chunk_ids = {node.source_chunk_id for node in nodes if node.source_chunk_id}
    chunks = {
        chunk.id: chunk
        for chunk in session.scalars(
            select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        ).all()
    } if chunk_ids else {}

    doc_ids = {node.document_id for node in nodes}
    documents = {
        doc.id: doc
        for doc in session.scalars(select(Document).where(Document.id.in_(doc_ids))).all()
    } if doc_ids else {}

    root = {"name": "知识库", "type": "root", "count": len(nodes), "children": []}
    by_document: dict[str, dict] = {}
    anchor_cache: dict[str, list[tuple[int, str]]] = {}

    for node in nodes:
        doc = documents.get(node.document_id)
        doc_name = _short_doc_name(doc.original_name) if doc else "未知文档"
        doc_node = by_document.get(node.document_id)
        if doc_node is None:
            doc_node = {"name": doc_name, "type": "doc", "count": 0, "children": {}}
            by_document[node.document_id] = doc_node

        # 按实体类型分组，比按章节更稳：章节号对读者没有信息量，类型能一眼看懂结构
        group_name = (node.entity_type or "其他").strip() or "其他"
        group = doc_node["children"].setdefault(
            group_name,
            {"name": group_name, "type": "chapter", "order": 0, "count": 0, "children": []},
        )
        group["children"].append(
            {
                "name": node.name,
                "type": "entity",
                "entity_type": node.entity_type,
                "degree": degree.get(node.id, 0),
            }
        )
        group["count"] += 1
        doc_node["count"] += 1

    total = 0
    for doc_node in by_document.values():
        groups = sorted(doc_node["children"].values(), key=lambda item: item["count"], reverse=True)
        kept_groups = []
        for group in groups:
            entities = sorted(group["children"], key=lambda item: item["degree"], reverse=True)
            group["children"] = entities[:MAX_ENTITY_PER_CHAPTER]
            group["truncated"] = max(0, group["count"] - len(group["children"]))
            group["name"] = f"{group['name']} · {group['count']}"
            if total >= limit:
                break
            kept_groups.append(group)
            total += len(group["children"])
        doc_node["children"] = kept_groups
        doc_node["name"] = f"{doc_node['name']} · {doc_node['count']}"
        root["children"].append(doc_node)

    root["children"].sort(key=lambda item: item["count"], reverse=True)
    return root


def to_html(tree: dict) -> str:
    import json
    from html import escape

    payload = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", payload).replace("__TITLE__", escape(tree.get("name", "知识树")))


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #fcfcfa;
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  #canvas { width: 100%; height: 100%; display: block; cursor: grab; }
  #canvas.dragging { cursor: grabbing; }
  .link { fill: none; stroke: #d8d5cc; stroke-width: 1.4; }
  .link.hot { stroke: #9db8f0; }
  .node rect { fill: #ffffff; stroke: #e2dfd6; stroke-width: 1; rx: 8; }
  .node.root rect { fill: #1f1e1b; stroke: #1f1e1b; }
  .node.doc rect { fill: #eef3fe; stroke: #c9d9fb; }
  .node.chapter rect { fill: #f6f5f1; stroke: #e2dfd6; }
  .node text { font-size: 12px; fill: #3d3b36; dominant-baseline: middle; }
  .node.root text { fill: #ffffff; font-weight: 600; }
  .node.doc text { fill: #1f4fd0; font-weight: 550; }
  .node .badge { font-size: 10px; fill: #8a8880; }
  .node.collapsed rect { stroke-dasharray: 3 2; }
  .node:hover rect { stroke: #2f6feb; }
  #hint { position: fixed; left: 12px; bottom: 10px; font-size: 11px; color: #a8a69f; }
</style>
</head>
<body>
<svg id="canvas"><g id="viewport"></g></svg>
<div id="hint">滚轮缩放 · 拖拽平移 · 点击节点折叠</div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("canvas");
const viewport = document.getElementById("viewport");
const collapsed = new Set();
let scale = 1, tx = 0, ty = 0;

function visibleChildren(node) {
  if (collapsed.has(node.__id)) return [];
  return node.children || [];
}

function layout() {
  let leaf = 0;
  const nodes = [], links = [];
  function walk(node, depth, parent) {
    // __id 由 assignIds 统一分配，布局不能覆盖它，否则折叠状态会失效
    const kids = visibleChildren(node);
    let y;
    if (!kids.length) { y = leaf * 30; leaf += 1; }
    else {
      const ys = kids.map((kid) => walk(kid, depth + 1, node));
      y = (ys[0] + ys[ys.length - 1]) / 2;
    }
    node.__x = depth * 190;
    node.__y = y;
    nodes.push(node);
    if (parent) links.push([parent, node]);
    return y;
  }
  walk(DATA, 0, null);
  return { nodes, links };
}

function label(text, max) {
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function render() {
  const { nodes, links } = layout();
  viewport.innerHTML = "";

  for (const [parent, child] of links) {
    const x1 = parent.__x + 150, y1 = parent.__y + 14;
    const x2 = child.__x, y2 = child.__y + 14;
    const path = document.createElementNS(NS, "path");
    const mid = (x1 + x2) / 2;
    path.setAttribute("d", `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`);
    path.setAttribute("class", "link");
    viewport.appendChild(path);
  }

  for (const node of nodes) {
    const g = document.createElementNS(NS, "g");
    const isCollapsed = collapsed.has(node.__id);
    g.setAttribute("class", `node ${node.type}` + (isCollapsed ? " collapsed" : ""));
    g.setAttribute("transform", `translate(${node.__x},${node.__y})`);

    const kids = node.children || [];
    const width = node.type === "root" ? 120 : node.type === "doc" ? 168 : 150;
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", 0); rect.setAttribute("y", 0);
    rect.setAttribute("width", width); rect.setAttribute("height", 28);
    rect.setAttribute("rx", 8);
    g.appendChild(rect);

    const text = document.createElementNS(NS, "text");
    text.setAttribute("x", 10); text.setAttribute("y", 15);
    const max = node.type === "root" ? 8 : node.type === "doc" ? 18 : 16;
    text.textContent = label(node.name, max);
    g.appendChild(text);

    if (kids.length) {
      const badge = document.createElementNS(NS, "text");
      badge.setAttribute("class", "badge");
      badge.setAttribute("x", width - 8);
      badge.setAttribute("y", 15);
      badge.setAttribute("text-anchor", "end");
      badge.textContent = isCollapsed ? `+${kids.length}` : `${kids.length}`;
      g.appendChild(badge);
    }

    g.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!kids.length) return;
      if (collapsed.has(node.__id)) collapsed.delete(node.__id);
      else collapsed.add(node.__id);
      render();
      fit();
    });
    viewport.appendChild(g);
  }
  applyTransform();
}

function applyTransform() {
  viewport.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
}

function fit() {
  const box = viewport.getBBox();
  const pad = 24;
  const w = svg.clientWidth, h = svg.clientHeight;
  scale = Math.min((w - pad * 2) / Math.max(1, box.width), (h - pad * 2) / Math.max(1, box.height), 1);
  tx = pad - box.x * scale;
  ty = pad - box.y * scale;
  applyTransform();
}

let dragging = false, lastX = 0, lastY = 0, moved = false;
svg.addEventListener("mousedown", (event) => {
  dragging = true; moved = false; lastX = event.clientX; lastY = event.clientY;
  svg.classList.add("dragging");
});
window.addEventListener("mousemove", (event) => {
  if (!dragging) return;
  const dx = event.clientX - lastX, dy = event.clientY - lastY;
  if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
  tx += dx; ty += dy; lastX = event.clientX; lastY = event.clientY;
  applyTransform();
});
window.addEventListener("mouseup", () => { dragging = false; svg.classList.remove("dragging"); });
svg.addEventListener("wheel", (event) => {
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.1 : 0.9;
  const rect = svg.getBoundingClientRect();
  const mx = event.clientX - rect.left, my = event.clientY - rect.top;
  tx = mx - (mx - tx) * factor;
  ty = my - (my - ty) * factor;
  scale *= factor;
  applyTransform();
}, { passive: false });

// 先给整棵树分配稳定 id，再决定默认折叠哪些层
(function assignIds(node) {
  node.__id = assignIds.counter = (assignIds.counter || 0) + 1;
  (node.children || []).forEach(assignIds);
})(DATA);

// 默认只展开到文档层：章节和实体收起，避免一进来就被缩放到看不清
// 默认展开到「实体类型」这一层，实体本身折叠，避免一屏几百个叶子
for (const doc of DATA.children || []) {
  for (const group of doc.children || []) collapsed.add(group.__id);
}
render();
fit();
</script>
</body>
</html>"""
