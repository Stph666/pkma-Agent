"""知识图谱可视化：生成一个自包含的 HTML（ECharts 力导向图）供 iframe 承载。

Gradio 会过滤 HTML 组件里的 script，因此这里把整页塞进 iframe 的 srcdoc，
脚本与样式都在 iframe 内生效，点击节点即在页内展示来源文档与原文片段。
"""

from __future__ import annotations

import json
from html import escape

ECHARTS_PATH = "/static/echarts.min.js"

TYPE_COLORS = {
    "技术": "#2563eb",
    "概念": "#7c3aed",
    "工具": "#0f9d58",
    "指标": "#d97706",
    "组织": "#db2777",
}
DEFAULT_COLOR = "#6e6e73"


def _template(nodes: list[dict], edges: list[dict], title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; overflow: hidden; background: #fbfbfd;
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
  #chart {{ width: 100%; height: calc(100vh - 120px); min-height: 360px; }}
  #detail {{ margin: 0 16px 16px; padding: 12px 14px; min-height: 56px;
    background: #fff; border: 1px solid #e8e8ed; border-radius: 12px;
    font-size: 13px; line-height: 1.7; color: #3a3a3c; }}
  #detail .heading {{ color: #1d1d1f; font-weight: 600; margin-bottom: 4px; }}
  #detail .meta {{ color: #86868b; font-size: 12px; margin-left: 8px; font-weight: 400; }}
  #detail .foot {{ margin-top: 8px; color: #86868b; font-size: 12px; }}
  #empty {{ padding: 60px 20px; text-align: center; color: #86868b; font-size: 13px; }}
</style>
</head>
<body>
<div id="chart"></div>
<div id="detail">点击节点或连线查看来源文档与原文片段。</div>
<script src="{ECHARTS_PATH}"></script>
<script>
const graph = {json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)};
const detail = document.getElementById('detail');

function show(text, meta, snippet, footer) {{
  detail.innerHTML = '<div class="heading">' + text + '<span class="meta">' + meta +
    '</span></div>' + (snippet ? '<div>' + snippet + '</div>' : '') +
    (footer ? '<div class="foot">' + footer + '</div>' : '');
}}

if (!graph.nodes.length) {{
  document.getElementById('chart').innerHTML =
    '<div id="empty">暂无图谱数据。上传文档并生成知识组织后，这里会显示实体与关系。</div>';
}} else {{
  const chart = echarts.init(document.getElementById('chart'));
  const nodes = graph.nodes.map(n => ({{
    id: n.id, name: n.name, category: n.type,
    // 关联度越高节点越大，视觉上先看到结构核心
    symbolSize: Math.min(34, 14 + 3 * Math.sqrt(n.degree || 0)),
    itemStyle: {{ color: {json.dumps(TYPE_COLORS)}[n.type] || "{DEFAULT_COLOR}" }},
    raw: n
  }}));
  const edges = graph.edges.map(e => ({{
    source: e.source, target: e.target, value: e.label || e.relation,
    label: {{ show: true, formatter: e.label || e.relation, fontSize: 10, color: '#86868b' }},
    lineStyle: {{ color: '#c7c7cc', curveness: 0.08, width: 1 }},
    raw: e
  }}));

  chart.setOption({{
    tooltip: {{
      trigger: 'item',
      formatter: p => {{
        if (p.dataType === 'edge') {{
          const raw = p.data.raw;
          const head = (raw.content || '').split(/[。！？\\n]/)[0];
          return raw.source_name + ' —' + (raw.label || raw.relation) + '→ ' + raw.target_name +
                 (head ? '<br/><span style="color:#86868b">' + head + '</span>' : '');
        }}
        return p.data.name + '（' + (p.data.raw.type || '实体') + '，关联 ' + (p.data.raw.degree || 0) + '）';
      }}
    }},
    series: [{{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      force: {{ repulsion: 150, edgeLength: 80, gravity: 0.12 }},
      zoom: 0.9,
      label: {{ show: true, position: 'right', fontSize: 11, color: '#1d1d1f' }},
      labelLayout: {{ hideOverlap: true }},
      lineStyle: {{ opacity: 0.7 }},
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 6,
      data: nodes, links: edges,
      emphasis: {{ focus: 'adjacency', lineStyle: {{ width: 2 }} }}
    }}]
  }});

  chart.on('click', params => {{
    if (params.dataType === 'node') {{
      const raw = params.data.raw;
      show(raw.name,
           (raw.type || '实体') + ' · 关联 ' + (raw.degree || 0) + ' · ' +
           (raw.filename || '') + ' ' + (raw.location || ''),
           raw.content || raw.snippet || '',
           '选中具体文档后点上方「查看该文档详情」，可看到完整切片与摘要');
    }} else if (params.dataType === 'edge') {{
      const raw = params.data.raw;
      show(raw.relation, raw.location || '', raw.snippet || '');
    }}
  }});
  window.addEventListener('resize', () => chart.resize());
}}
</script>
</body>
</html>"""


def build_graph_html(nodes: list[dict], edges: list[dict], title: str = "知识图谱") -> str:
    """返回可直接放进 gr.HTML 的 iframe 片段。"""
    document = _template(nodes, edges, title)
    return (
        '<iframe title="知识图谱" style="width:100%;height:clamp(420px,64vh,720px);border:0;'
        'border-radius:12px;background:#fbfbfd;" srcdoc="'
        + escape(document, quote=True)
        + '"></iframe>'
    )
