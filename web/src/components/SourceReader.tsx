import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { api } from "../api";

export type SourceDetail = {
  title: string;
  meta: string;
  content: string;
  documentId?: string;
  page?: number | null;
  keyword?: string;
  chunkId?: string;
};

type Props = {
  source: SourceDetail | null;
  onClose: () => void;
};

/** 应用内阅读面板：图谱/引用点开就在右侧展开，不跳新标签页。 */
export default function SourceReader({ source, onClose }: Props) {
  const [body, setBody] = useState("");
  const [meta, setMeta] = useState("");

  useEffect(() => {
    if (!source) return;
    setBody(source.content);
    setMeta(source.meta);
    if (!source.chunkId) return;
    let alive = true;
    api
      .chunkContext(source.chunkId)
      .then((data) => {
        if (!alive) return;
        setBody(data.text || source.content);
        setMeta([source.title, data.filename, data.location].filter(Boolean).join(" · "));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [source]);

  useEffect(() => {
    if (!source) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [source, onClose]);

  if (!source) return null;

  return (
    <div className="reader-overlay" onMouseDown={onClose}>
      <aside className="reader-panel" onMouseDown={(event) => event.stopPropagation()}>
        <header className="reader-head">
          <div>
            <h3>{source.title}</h3>
            <p>{meta}</p>
          </div>
          <div className="reader-actions">
            <button className="icon-chip" title="关闭" onClick={onClose}>
              <X size={16} />
            </button>
          </div>
        </header>
        <pre className="reader-body">{highlight(body, source.keyword)}</pre>
      </aside>
    </div>
  );
}

function highlight(text: string, keyword?: string) {
  if (!keyword || keyword.length < 2) return text;
  const parts = text.split(new RegExp(`(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"));
  return parts.map((piece, index) =>
    piece.toLowerCase() === keyword.toLowerCase() ? <mark key={index}>{piece}</mark> : piece,
  );
}
