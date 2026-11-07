"""关系图谱：在抽取出的三元组之外，补两类「结构性关系」，再用 Cytoscape 渲染。

抽取受限于成本和提示词上限，天然稀疏（我们这份数据平均度 <1）。
成熟图产品通常会把「共现」「同义」这类低成本关系也画出来，让孤岛连成片：

- 共现：同一片段里出现的实体两两连边（虚线、可关闭）
- 同名：不同文档里同名实体连边（用于跨文档串联）
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, GraphEdge, GraphNode


def build_payload(
    session: Session,
    document_id: str | None = None,
    types: list[str] | None = None,
    limit: int = 240,
    include_cooccur: bool = True,
    include_alias: bool = True,
    min_degree: int = 0,
) -> dict:
    stmt = select(GraphNode)
    if document_id:
        stmt = stmt.where(GraphNode.document_id == document_id)
    if types:
        stmt = stmt.where(GraphNode.entity_type.in_(types))
    nodes = list(session.scalars(stmt).all())

    node_ids = {node.id for node in nodes}
    edges = [
        edge
        for edge in session.scalars(select(GraphEdge)).all()
        if edge.source_node_id in node_ids and edge.target_node_id in node_ids
    ]

    degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        degree[edge.source_node_id] += 1
        degree[edge.target_node_id] += 1

    items: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    for edge in edges:
        key = tuple(sorted((edge.source_node_id, edge.target_node_id)))
        seen_pairs.add(key)
        items.append(
            {
                "data": {
                    "id": edge.id,
                    "source": edge.source_node_id,
                    "target": edge.target_node_id,
                    "label": edge.relation,
                    "kind": "relation",
                }
            }
        )

    if include_cooccur:
        by_chunk: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            if node.source_chunk_id:
                by_chunk[node.source_chunk_id].append(node.id)
        for entity_ids in by_chunk.values():
            if not 2 <= len(entity_ids) <= 8:
                continue
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    key = tuple(sorted((entity_ids[i], entity_ids[j])))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    degree[key[0]] += 1
                    degree[key[1]] += 1
                    items.append(
                        {
                            "data": {
                                "id": f"co-{key[0]}-{key[1]}",
                                "source": key[0],
                                "target": key[1],
                                "label": "共现",
                                "kind": "cooccur",
                            }
                        }
                    )

    if include_alias:
        by_name: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            by_name[node.normalized_name].append(node.id)
        for entity_ids in by_name.values():
            for i in range(1, len(entity_ids)):
                key = tuple(sorted((entity_ids[0], entity_ids[i])))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                degree[key[0]] += 1
                degree[key[1]] += 1
                items.append(
                    {
                        "data": {
                            "id": f"alias-{key[0]}-{key[1]}",
                            "source": key[0],
                            "target": key[1],
                            "label": "同名",
                            "kind": "alias",
                        }
                    }
                )

    # 孤立点不进主图（否则会被力导向排成网格，视觉噪音），只在统计里报数量
    ranked = [
        node for node in nodes if degree.get(node.id, 0) >= max(1, min_degree)
    ]
    ranked.sort(key=lambda node: degree.get(node.id, 0), reverse=True)
    ranked = ranked[:limit]
    ranked_ids = {node.id for node in ranked}

    chunk_ids = {node.source_chunk_id for node in ranked if node.source_chunk_id}
    chunks = {
        chunk.id: chunk
        for chunk in session.scalars(
            select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        ).all()
    } if chunk_ids else {}
    doc_names = {
        doc.id: doc.original_name
        for doc in session.scalars(select(Document)).all()
    }

    node_items = [
        {
            "data": {
                "id": node.id,
                "label": node.name,
                "type": node.entity_type,
                "degree": degree.get(node.id, 0),
                "doc": doc_names.get(node.document_id, ""),
                "chunkId": node.source_chunk_id,
                "documentId": node.document_id,
                "chunkId": node.source_chunk_id,
                "page": (chunks.get(node.source_chunk_id).page_start if chunks.get(node.source_chunk_id) else None),
                "content": (chunks.get(node.source_chunk_id).content[:6000] if chunks.get(node.source_chunk_id) else ""),
            }
        }
        for node in ranked
    ]

    kept_edges = [
        item
        for item in items
        if item["data"]["source"] in ranked_ids and item["data"]["target"] in ranked_ids
    ]

    kinds = defaultdict(int)
    for item in kept_edges:
        kinds[item["data"]["kind"]] += 1

    return {
        "nodes": node_items,
        "edges": kept_edges,
        "stats": {
            "entities": len(node_items),
            "relations": kinds.get("relation", 0),
            "cooccur": kinds.get("cooccur", 0),
            "alias": kinds.get("alias", 0),
            "isolated": sum(1 for node in ranked if degree.get(node.id, 0) == 0),
            "avg_degree": round(sum(degree.get(node.id, 0) for node in ranked) * 2 / max(1, len(ranked)), 2),
        },
    }


def to_html(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #fcfcfa;
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  #cy { position: absolute; inset: 0; }
  #bar { position: absolute; left: 10px; top: 10px; display: flex; gap: 6px; flex-wrap: wrap;
    background: rgba(255,255,255,.92); border: 1px solid #e6e4dd; border-radius: 10px;
    padding: 6px 8px; font-size: 11.5px; color: #4d4b46; backdrop-filter: blur(6px); }
  #bar button { border: 1px solid #e6e4dd; background: #fff; border-radius: 999px;
    padding: 3px 9px; font-size: 11.5px; color: #4d4b46; cursor: pointer; }
  #bar button.on { background: #eef3fe; border-color: #c9d9fb; color: #1f4fd0; }
  #legend { position: absolute; right: 10px; top: 10px; background: rgba(255,255,255,.92);
    border: 1px solid #e6e4dd; border-radius: 10px; padding: 8px 10px; font-size: 11px; color: #6b6960; }
  #legend div { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  #legend i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  #info { position: absolute; left: 10px; bottom: 10px; width: min(560px, 62%);
    max-height: 62%; display: none; flex-direction: column;
    background: #ffffff; border: 1px solid #e6e4dd; border-radius: 12px;
    padding: 12px 14px; font-size: 12.5px; color: #4d4b46;
    box-shadow: 0 10px 30px rgba(0,0,0,.08); }
  .info-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  #info h4 { margin: 0; font-size: 13.5px; color: #1f1e1b; }
  .info-meta { font-size: 11.5px; color: #9a9890; }
  .info-body { margin: 8px 0 0; flex: 1; overflow: auto; white-space: pre-wrap;
    word-break: break-word; line-height: 1.7; font-family: inherit; font-size: 12.5px; }
  .info-open { margin-top: 8px; font-size: 12px; color: #2f6feb; text-decoration: none; }
  .info-open:hover { text-decoration: underline; }
  #busy { position: absolute; inset: 0; display: none; align-items: center; justify-content: center;
    background: rgba(255,255,255,.72); font-size: 12.5px; color: #6b6960; z-index: 5; }
  #hint { position: absolute; right: 10px; bottom: 8px; font-size: 10.5px; color: #a8a69f; }
</style>
</head>
<body>
<div id="cy"></div>
<div id="bar">
  <button id="btn-cooccur" class="on">共现边</button>
  <button id="btn-labels">关系标签</button>
  <button id="btn-fit">适配</button>
  <button id="btn-layout">重新布局</button>
  <span id="stat" style="align-self:center;color:#a8a69f"></span>
</div>
<div id="legend"></div>
<div id="info">
  <div class="info-head"><h4></h4><span class="info-meta"></span></div>
  <pre class="info-body"></pre>
  <a class="info-open" target="_blank" rel="noreferrer">在新标签打开原文片段</a>
</div>
<div id="busy">布局计算中…</div>
<div id="hint">滚轮缩放 · 拖拽平移 · 悬停看邻居 · 点击看详情</div>
<script src="/static/cytoscape.min.js"></script>
<script src="/static/layout-base.js"></script>
<script src="/static/cose-base.js"></script>
<script src="/static/cytoscape-fcose.js"></script>
<script>
window.onerror = (message, source, line, col) => { window.__err = `${message} @${line}:${col}`; };
const DATA = __DATA__;
const TYPE_COLOR = {
  "概念": "#2f6feb", "技术": "#0f9d58", "工具": "#b26a00",
  "指标": "#8e44ad", "组织": "#c0392b", "方法": "#00838f"
};
const color = (type) => TYPE_COLOR[type] || "#6b7280";

// 标签策略：取度数最高的前 25 个常显，其余悬停时再显示，避免一屏全是文字
const degrees = DATA.nodes.map((n) => n.data.degree).sort((a, b) => b - a);
const LABEL_MIN = degrees[Math.min(24, Math.max(0, degrees.length - 1))] || 2;
const showLabel = (ele) => (ele.data("degree") >= LABEL_MIN ? ele.data("label") : "");

const cy = cytoscape({
  container: document.getElementById("cy"),
  elements: DATA.nodes.concat(DATA.edges),
  wheelSensitivity: 0.22,
  style: [
    { selector: "node", style: {
        "background-color": (ele) => color(ele.data("type")),
        "width": (ele) => 16 + Math.min(22, ele.data("degree") * 2.4),
        "height": (ele) => 16 + Math.min(22, ele.data("degree") * 2.4),
        "label": showLabel,
        "font-size": 10,
        "color": "#3d3b36",
        "text-valign": "bottom",
        "text-margin-y": 4,
        "min-zoomed-font-size": 0,
        "border-width": 1.5,
        "border-color": "#ffffff"
    }},
    { selector: "edge", style: {
        "width": 1.1,
        "line-color": "#d8d5cc",
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": "#d8d5cc",
        "arrow-scale": 0.7,
        "label": "data(label)",
        "font-size": 8,
        "color": "#a8a69f",
        "text-rotation": "autorotate",
        "min-zoomed-font-size": 0
    }},
    { selector: "edge[kind = 'cooccur']", style: {
        "line-style": "dashed", "line-color": "#e2dfd6",
        "target-arrow-shape": "none", "width": 0.9
    }},
    { selector: "edge[kind = 'alias']", style: {
        "line-color": "#c9d9fb", "target-arrow-color": "#c9d9fb", "width": 1.4
    }},
    { selector: ".dim", style: { "opacity": 0.15 } },
    { selector: "node.hot", style: { "border-width": 3, "border-color": "#1f1e1b" } },
    { selector: "edge.hot", style: { "line-color": "#2f6feb", "target-arrow-color": "#2f6feb", "width": 2, "color": "#1f4fd0" } },
    { selector: ".hidden", style: { "display": "none" } }
  ]
});

window.cy = cy;  // 便于自动化测试与调试

// draft 会把大图排成一条斜线，质量不可用；统一用 default，靠遮罩提示等待
const fcose = cy.layout({
  name: "fcose",
  quality: "default",
  animate: false,
  randomize: true,  // fcose 需要初始位置，设 false 会抛 nodeIndexes 错误
  nodeRepulsion: 14000,
  idealEdgeLength: 70,
  edgeElasticity: 0.2,
  gravity: 0.35,
  numIter: 1800,
  nodeSeparation: 90
});
fcose.run();
cy.fit(undefined, 40);

// 统计与图例
const stats = DATA.stats;
document.getElementById("stat").textContent =
  `${stats.entities} 实体 · ${stats.relations} 关系 · ${stats.cooccur} 共现 · 平均度 ${stats.avg_degree}`;
const legend = document.getElementById("legend");
const types = [...new Set(DATA.nodes.map((n) => n.data.type))];
legend.innerHTML = "<div style='margin-bottom:4px;color:#a8a69f'>实体类型</div>" +
  types.map((t) => `<div><i style="background:${color(t)}"></i>${t}</div>`).join("");

// 悬停高亮邻居：批处理 + 指针离开画布强制清除（否则移到 HTML 图例上会卡住不动）
const HEAVY = DATA.nodes.length > 120;  // 大图不做全量变暗，否则 hover 一次要重算 600+ 元素
let hovered = null;
const clearHighlight = () => {
  if (!hovered) return;
  hovered = null;
  cy.batch(() => cy.elements().removeClass("dim hot"));
  if (!labelsOn) {
    cy.batch(() => {
      cy.nodes().style("label", showLabel);
      cy.edges().style("label", "");
    });
  }
};

cy.on("mouseover", "node", (event) => {
  if (HEAVY) return;
  const node = event.target;
  if (node === hovered) return;
  hovered = node;
  const neighborhood = node.closedNeighborhood();
  cy.batch(() => {
    cy.elements().addClass("dim");
    neighborhood.removeClass("dim");
    node.addClass("hot");
    node.connectedEdges().addClass("hot");
    neighborhood.nodes().style("label", "data(label)");
    node.connectedEdges().style("label", "data(label)");
  });
});

cy.on("mouseout", "node", clearHighlight);
cy.container().addEventListener("mouseleave", clearHighlight);

// 点击看详情
const info = document.getElementById("info");
window.showNodeInfo = (d) => {
  // 交给应用内右侧阅读面板：能带页码、能高亮，比新开标签页好定位
  const meta = [d.type, d.doc, d.page ? `第 ${d.page} 页` : "", `连接度 ${d.degree}`]
    .filter(Boolean).join(" · ");
  window.parent.postMessage(
    {
      type: "open-source",
      title: d.label,
      meta,
      content: d.content || "（这个实体没有关联片段）",
      documentId: d.documentId,
      chunkId: d.chunkId,
      page: d.page ?? null,
      keyword: d.label,
    },
    "*"
  );
};
cy.on("tap", "node", (event) => window.showNodeInfo(event.target.data()));
cy.on("tap", (event) => { if (event.target === cy) info.style.display = "none"; });

// 工具栏
const btnCo = document.getElementById("btn-cooccur");
btnCo.onclick = () => {
  const on = btnCo.classList.toggle("on");
  cy.edges("[kind = 'cooccur']").style("display", on ? "element" : "none");
};
let labelsOn = false;
const btnLabels = document.getElementById("btn-labels");
btnLabels.classList.remove("on");
btnLabels.onclick = () => {
  labelsOn = btnLabels.classList.toggle("on");
  if (labelsOn) {
    cy.nodes().style("label", "data(label)");
    cy.edges().style("label", "data(label)");
  } else {
    cy.nodes().style("label", showLabel);
    cy.edges().style("label", "");
  }
};
document.getElementById("btn-fit").onclick = () => cy.fit(undefined, 40);
const busy = document.getElementById("busy");
const runLayout = () => {
  busy.style.display = "flex";
  fcose.one("layoutstop", () => {
    busy.style.display = "none";
    cy.fit(undefined, 40);
  });
  fcose.run();
};
runLayout();
document.getElementById("btn-layout").onclick = runLayout;
</script>
</body>
</html>"""
