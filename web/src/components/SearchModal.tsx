import { useEffect, useMemo, useRef, useState } from "react";
import { MessageSquare, Search, X } from "lucide-react";
import { api, type SearchHit } from "../api";

type Props = {
  open: boolean;
  onClose: () => void;
  onOpenConversation: (id: string) => void;
};

export default function SearchModal({ open, onClose, onOpenConversation }: Props) {
  const [query, setQuery] = useState("");
  const [talks, setTalks] = useState<{ id: string; title: string }[]>([]);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setHits([]);
    setTalks([]);
    const timer = setTimeout(() => inputRef.current?.focus(), 40);
    return () => clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    const keyword = query.trim();
    if (!keyword) {
      setTalks([]);
      setHits([]);
      return;
    }
    setLoading(true);
    const timer = setTimeout(() => {
      api
        .search(keyword)
        .then((data) => {
          setTalks(data.conversations);
          setHits(data.messages);
        })
        .catch(() => {
          setTalks([]);
          setHits([]);
        })
        .finally(() => setLoading(false));
    }, 220);
    return () => clearTimeout(timer);
  }, [query, open]);

  const empty = useMemo(
    () => !loading && query.trim() && !talks.length && !hits.length,
    [loading, query, talks.length, hits.length],
  );

  if (!open) return null;

  return (
    <div className="search-overlay" onMouseDown={onClose}>
      <div className="search-panel" onMouseDown={(event) => event.stopPropagation()}>
        <div className="search-head">
          <Search size={16} className="search-icon" />
          <input
            ref={inputRef}
            value={query}
            placeholder="搜索对话标题或聊天内容"
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && (
            <button className="search-clear" onClick={() => setQuery("")}>
              清空
            </button>
          )}
          <button className="search-close" onClick={onClose} title="关闭">
            <X size={16} />
          </button>
        </div>

        <div className="search-body">
          {!query.trim() && <p className="search-hint">输入关键词，同时搜索对话名与每条消息。</p>}
          {loading && <p className="search-hint">搜索中…</p>}
          {empty && <p className="search-hint">没有找到「{query}」相关的对话或消息。</p>}

          {talks.length > 0 && (
            <>
              <div className="search-label">对话</div>
              {talks.map((item) => (
                <button
                  key={item.id}
                  className="search-row"
                  onClick={() => {
                    onOpenConversation(item.id);
                    onClose();
                  }}
                >
                  <MessageSquare size={14} className="search-row-icon" />
                  <span className="search-row-title">{highlight(item.title, query)}</span>
                </button>
              ))}
            </>
          )}

          {hits.length > 0 && (
            <>
              <div className="search-label">消息</div>
              {hits.map((item) => (
                <button
                  key={item.id}
                  className="search-row"
                  onClick={() => {
                    onOpenConversation(item.conversation_id);
                    onClose();
                  }}
                >
                  <MessageSquare size={14} className="search-row-icon" />
                  <span className="search-row-text">
                    <span className="search-row-title">{item.title}</span>
                    <span className="search-row-snippet">
                      {item.role === "user" ? "你：" : "助手："}
                      {highlight(item.snippet, query)}
                    </span>
                  </span>
                  <span className="search-row-time">{item.created_at.slice(11, 16)}</span>
                </button>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function highlight(text: string, keyword: string) {
  const word = keyword.trim();
  if (!word) return text;
  const parts = text.split(new RegExp(`(${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"));
  return parts.map((piece, index) =>
    piece.toLowerCase() === word.toLowerCase() ? <mark key={index}>{piece}</mark> : piece,
  );
}
