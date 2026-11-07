import { useEffect, useRef } from "react";
// 只引图谱需要的模块：整包 echarts 会把地图/3D/统计图一起打进来（1MB+）
import * as echarts from "echarts/core";
import { GraphChart } from "echarts/charts";
import { LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer]);
import type { SourceDetail } from "./SourceReader";

type GraphNode = {
  data: {
    id: string;
    label: string;
    type?: string;
    degree?: number;
    doc?: string;
    documentId?: string;
    chunkId?: string;
    page?: number | null;
    content?: string;
  };
};

type GraphEdge = {
  data: {
    source: string;
    target: string;
    label?: string;
    relation?: string;
    kind?: string;
    content?: string;
    snippet?: string;
    location?: string;
    document_id?: string;
    source_name?: string;
    target_name?: string;
  };
};

type Props = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onOpenSource: (detail: SourceDetail) => void;
  focusId?: string;
};

const TYPE_COLOR: Record<string, string> = {
  概念: "#2f6feb",
  技术: "#0f9d58",
  工具: "#b26a00",
  指标: "#8e44ad",
  组织: "#c0392b",
  方法: "#00838f",
};

/**
 * 关系图谱画布：直接复刻 ECharts 官方 graph 示例的力导向方案。
 * 相比 cytoscape + fcose，力导向是逐帧迭代、不阻塞主线程，240 节点也不会卡。
 */
export default function GraphCanvas({ nodes, edges, onOpenSource, focusId }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    // 便于自动化核对节点位置（无副作用）
    (ref.current as unknown as { __chart?: echarts.ECharts }).__chart = chart;

    let allLabels = false;
    const categories = Array.from(new Set(nodes.map((node) => node.data.type || "其他")));
    const degrees = nodes.map((node) => node.data.degree || 0).sort((a, b) => b - a);
    // 一屏 200+ 实体时全部显示会糊成一片：默认只给关联度前 40 的标签，
    // 放大到 1.5 倍后再全部显示（下面 graphroam 里切换）
    const labelThreshold = degrees[Math.min(39, Math.max(0, degrees.length - 1))] || 2;

    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        formatter: (params: any) => {
          if (params.dataType === "edge") {
            const raw = params.data.raw as GraphEdge["data"];
            return `${raw.source_name || ""} —${raw.label || raw.relation || "关联"}→ ${raw.target_name || ""}`;
          }
          const raw = params.data.raw as GraphNode["data"];
          return `${raw.label}（${raw.type || "实体"} · 连接度 ${raw.degree ?? 0}）`;
        },
      },
      legend: {
        data: categories,
        top: 12,
        right: 16,
        orient: "vertical",
        textStyle: { fontSize: 11, color: "#6b6960" },
        itemWidth: 9,
        itemHeight: 9,
        itemGap: 6,
      },
      series: [
        {
          type: "graph",
          layout: "force",
          roam: true,
          draggable: true,
          categories: categories.map((name) => ({
            name,
            itemStyle: { color: TYPE_COLOR[name] || "#6b7280" },
          })),
          force: { repulsion: 420, edgeLength: 120, gravity: 0.06, friction: 0.12 },
          top: 52, left: 28, right: 92, bottom: 28,
          zoom: 0.86,
          label: {
            show: true,
            position: "right",
            fontSize: 9.5,
            color: "#3d3b36",
            textBorderColor: "rgba(255,255,255,0.9)",
            textBorderWidth: 2,
            formatter: (params: any) =>
              allLabels || (params.data.raw?.degree ?? 0) >= labelThreshold
                ? params.data.raw?.label || ""
                : "",
          },
          labelLayout: { hideOverlap: false, moveOverlap: "shiftY" },
          lineStyle: { color: "#d8d5cc", width: 1, curveness: 0.06, opacity: 0.9 },
          edgeSymbol: ["none", "arrow"],
          edgeSymbolSize: 5,
          edgeLabel: {
            show: false,
            formatter: (params: any) => params.data.raw?.label || "",
            fontSize: 9,
            color: "#9a9890",
          },
          emphasis: {
            focus: "adjacency",
            lineStyle: { width: 2, color: "#9fb6e8" },
            label: { show: true },
          },
          data: nodes.map((node) => ({
            id: node.data.id,
            name: node.data.label,
            category: node.data.type || "其他",
            symbolSize: Math.min(30, 12 + 3 * Math.sqrt(node.data.degree || 0)),
            raw: node.data,
          })),
          links: edges.map((edge) => ({
            source: edge.data.source,
            target: edge.data.target,
            value: edge.data.label || edge.data.relation || "",
            raw: edge.data,
            lineStyle: edge.data.kind === "cooccur" ? { type: "dashed", opacity: 0.45 } : undefined,
          })),
        },
      ],
    });

    // 力导向稳定后自动缩放到刚好装下，避免节点被裁在边界外
    const fitTimer = window.setTimeout(() => {
      const model = (chart as unknown as { getModel: () => any }).getModel();
      const data = model.getSeriesByIndex(0).getData();
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (let index = 0; index < data.count(); index += 1) {
        const point = data.getItemLayout(index) as number[] | undefined;
        if (!point) continue;
        minX = Math.min(minX, point[0]); maxX = Math.max(maxX, point[0]);
        minY = Math.min(minY, point[1]); maxY = Math.max(maxY, point[1]);
      }
      if (!Number.isFinite(minX)) return;
      const box = ref.current?.getBoundingClientRect();
      if (!box) return;
      const zoom = Math.min(
        1.15,
        (box.width - 120) / Math.max(1, maxX - minX),
        (box.height - 130) / Math.max(1, maxY - minY),
      );
      chart.setOption({
        series: [{ center: [(minX + maxX) / 2, (minY + maxY) / 2], zoom }],
      });
    }, 1500);

    chart.on("click", (params: any) => {
      if (params.dataType === "node") {
        const raw = params.data.raw as GraphNode["data"];
        onOpenSource({
          title: raw.label,
          meta: [raw.type, raw.doc, raw.page ? `第 ${raw.page} 页` : "", `连接度 ${raw.degree ?? 0}`]
            .filter(Boolean)
            .join(" · "),
          content: raw.content || "（这个实体没有关联片段）",
          documentId: raw.documentId,
          chunkId: raw.chunkId,
          page: raw.page ?? null,
          keyword: raw.label,
        });
        return;
      }
      if (params.dataType === "edge") {
        const raw = params.data.raw as GraphEdge["data"];
        onOpenSource({
          title: `${raw.source_name || ""} —${raw.label || raw.relation || "关联"}→ ${raw.target_name || ""}`,
          meta: raw.location || "",
          content: raw.content || raw.snippet || "（这条关系没有关联片段）",
          documentId: raw.document_id,
          page: null,
          keyword: undefined,
        });
      }
    });

    // 放大后放开全部标签：看整体时清爽，凑近看时信息完整
    chart.on("graphroam", () => {
      const option = chart.getOption() as { series?: Array<{ zoom?: number }> };
      const zoom = Number(option.series?.[0]?.zoom ?? 1);
      const next = zoom >= 1.5;
      if (next === allLabels) return;
      allLabels = next;
      chart.setOption({
        series: [
          {
            label: {
              formatter: (params: any) =>
                allLabels || (params.data.raw?.degree ?? 0) >= labelThreshold
                  ? params.data.raw?.label || ""
                  : "",
            },
          },
        ],
      });
    });

    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);

    return () => {
      window.clearTimeout(fitTimer);
      window.removeEventListener("resize", resize);
      observer.disconnect();
      chartRef.current = null;
      chart.dispose();
    };
  }, [nodes, edges, onOpenSource]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !focusId) return;
    const model = (chart as unknown as { getModel: () => any }).getModel();
    const data = model.getSeriesByIndex(0).getData();
    let index = -1;
    for (let i = 0; i < data.count(); i += 1) {
      if (data.getId(i) === focusId) {
        index = i;
        break;
      }
    }
    if (index < 0) return;
    const point = data.getItemLayout(index) as number[] | undefined;
    chart.dispatchAction({ type: "downplay", seriesIndex: 0 });
    chart.dispatchAction({ type: "highlight", seriesIndex: 0, dataIndex: index });
    if (point) {
      chart.setOption({ series: [{ center: point, zoom: 1.2 }] });
    }
  }, [focusId, nodes]);

  return <div className="graph-canvas" ref={ref} />;
}
