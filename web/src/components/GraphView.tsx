import { useEffect, useMemo, useState } from "react";
import { GitBranch, ListTree, Search } from "lucide-react";
import { api } from "../api";
import { Suspense, lazy } from "react";

// 图谱页按需加载：不进首屏包
const GraphCanvas = lazy(() => import("./GraphCanvas"));
import type { DocumentItem } from "../types";

type Props = {
  onOpenSource?: (detail: {
    title: string;
    meta: string;
    content: string;
    documentId?: string;
    chunkId?: string;
    page?: number | null;
    keyword?: string;
  }) => void; documents: DocumentItem[] };

const RANGES = [
  { value: "0", label: "自动（最多 240 个）" },
  { value: "40", label: "主要实体（前 40）" },
  { value: "-1", label: "全部（含孤立）" },
];

export default function GraphView({ documents, onOpenSource }: Props) {
  const [docId, setDocId] = useState("");
  const [query, setQuery] = useState("");
  const [range, setRange] = useState("0");
  const [mode, setMode] = useState<"mindmap" | "explore">("explore");
  const [html, setHtml] = useState("");
  const [graph, setGraph] = useState<{ nodes: { data: any }[]; edges: { data: any }[] } | null>(null);
  // 搜索框只在本地定位，不重新请求接口
  const focusNode = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword || !graph) return null;
    return graph.nodes.find((node) => String(node.data.label || "").toLowerCase().includes(keyword)) ?? null;
  }, [query, graph]);
  const [info, setInfo] = useState("");
  const [loading, setLoading] = useState(true);

  // 图谱 iframe 里点节点 → 交给应用内阅读面板，而不是跳新标签页
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const payload = event.data;
      if (!payload || payload.type !== "open-source") return;
      onOpenSource?.({
        title: payload.title || "",
        meta: payload.meta || "",
        content: payload.content || "",
        documentId: payload.documentId,
        chunkId: payload.chunkId,
        page: payload.page ?? null,
        keyword: payload.keyword,
      });
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [onOpenSource]);

  useEffect(() => {
    const timer = setTimeout(async () => {
      const params = new URLSearchParams();
      if (docId) params.set("document_id", docId);
      if (mode === "explore") {
        params.set("limit", range === "-1" ? "600" : range === "0" ? "240" : range);
      } else {
        params.set("limit", "300");
      }
      setLoading(true);
      try {
        if (mode === "explore") {
          const data = await api.graphExploreData(params);
          setGraph({ nodes: data.nodes, edges: data.edges });
          setInfo(
            `${data.stats.entities} 实体 · ${data.stats.relations} 关系 · ${data.stats.cooccur} 共现`,
          );
        } else {
          const data = await api.graphMindmap(params);
          setHtml(data.html);
          setInfo(`知识树 · ${data.entities} 个实体`);
        }
      } catch (error) {
        setInfo(`图谱读取失败：${(error as Error).message}`);
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [docId, range, mode]);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>知识图谱</h1>
          <p>实体与关系来自文档抽取，按关联度排序；点击节点可查看来源片段。</p>
        </div>
      </header>

      <div className="toolbar">
        <div className="seg">
          <button
            className={mode === "mindmap" ? "seg-item on" : "seg-item"}
            onClick={() => setMode("mindmap")}
          >
            <ListTree size={13} /> 思维导图
          </button>
          <button
            className={mode === "explore" ? "seg-item on" : "seg-item"}
            onClick={() => setMode("explore")}
          >
            <GitBranch size={13} /> 关系图谱
          </button>
        </div>
        <select value={docId} onChange={(event) => setDocId(event.target.value)}>
          <option value="">全部文档</option>
          {documents.map((item) => (
            <option key={item.id} value={item.id}>
              {item.original_name}
            </option>
          ))}
        </select>

        <div className="graph-search">
          <Search size={14} />
          <input
            value={query}
            placeholder="搜索实体，回车定位"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>

        <select value={range} onChange={(event) => setRange(event.target.value)}>
          {RANGES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <span className="hint-right">{info}</span>
      </div>

      <div className="graph-card">
        {loading && <div className="skeleton-block" />}
        {!loading && mode === "explore" && graph && graph.nodes.length > 0 && (
          <Suspense fallback={<div className="skeleton-block" />}>
            <GraphCanvas
              nodes={graph.nodes}
              edges={graph.edges}
              onOpenSource={onOpenSource ?? (() => {})}
              focusId={focusNode?.data.id}
            />
          </Suspense>
        )}
        {!loading && mode === "mindmap" && html && (
          <iframe title="知识树" className="graph-frame" srcDoc={html} />
        )}
        {!loading && ((mode === "explore" && (!graph || !graph.nodes.length)) || (mode === "mindmap" && !html)) && (
          <p className="sources-note">暂无图谱数据，先在知识库生成摘要与图谱。</p>
        )}
      </div>
    </div>
  );
}
