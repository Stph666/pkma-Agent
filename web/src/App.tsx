import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Library,
  MessageCircle,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PenLine,
  Search,
} from "lucide-react";
import { api } from "./api";
import ChatView from "./components/ChatView";
import GraphView from "./components/GraphView";
import KbView from "./components/KbView";
import SearchModal from "./components/SearchModal";
import SourceReader, { type SourceDetail } from "./components/SourceReader";
import LogoMark from "./components/LogoMark";
import type { Conversation, DocumentItem, Health } from "./types";

type View = "chat" | "kb" | "graph";

const NAV: { key: View; label: string; icon: typeof MessageCircle }[] = [
  { key: "chat", label: "智能问答", icon: MessageCircle },
  { key: "kb", label: "知识库", icon: Library },
  { key: "graph", label: "知识图谱", icon: Network },
];

function groupByDate(items: Conversation[]): { label: string; items: Conversation[] }[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 3600 * 1000;
  const groups: Record<string, Conversation[]> = { 今天: [], 昨天: [], 更早: [] };
  for (const item of items) {
    const time = new Date(item.updated_at).getTime();
    if (time >= startOfToday) groups["今天"].push(item);
    else if (time >= startOfYesterday) groups["昨天"].push(item);
    else groups["更早"].push(item);
  }
  return Object.entries(groups)
    .filter(([, value]) => value.length)
    .map(([label, value]) => ({ label, items: value }));
}

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [railOpen, setRailOpen] = useState(true);
  const [panelOpen, setPanelOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [hasMessages, setHasMessages] = useState(false);
  const [toast, setToast] = useState("");
  // 收起态：鼠标移到 logo 上时换成展开图标
  const [logoHover, setLogoHover] = useState(false);
  const [source, setSource] = useState<SourceDetail | null>(null);
  // 双击会话标题就地改名
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const loadDocuments = useCallback(async () => {
    try {
      const data = await api.documents();
      setDocuments(data.items);
      setCounts(data.counts || {});
    } catch {
      setDocuments([]);
    }
  }, []);

  const loadConversations = useCallback(async () => {
    try {
      const data = await api.conversations();
      setConversations(data.items);
    } catch {
      setConversations([]);
    }
  }, []);

  useEffect(() => {
    void loadDocuments();
    void loadConversations();
    api.health().then(setHealth).catch(() => setHealth(null));
  }, [loadDocuments, loadConversations]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 2400);
    return () => clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const readyDocuments = useMemo(
    () => documents.filter((item) => item.status === "ready"),
    [documents],
  );
  // 只显示有消息的会话，并按标题归一化去重（数据库里可能残留同题的测试会话）
  const visible = useMemo(() => {
    const seen = new Set<string>();
    return conversations
      .filter((item) => (item.message_count ?? 0) > 0)
      .filter((item) => {
        const key = (item.title || "").trim().replace(/[\s？?！!。，,：:]+$/g, "").toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }, [conversations]);
  const groups = useMemo(() => groupByDate(visible), [visible]);

  async function newChat() {
    if (view === "chat" && conversationId && !hasMessages) {
      setToast("已在新会话中");
      return;
    }
    try {
      const created = await api.createConversation();
      await loadConversations();
      setConversationId(created.id);
      setHasMessages(false);
      setView("chat");
    } catch (error) {
      setToast(`新建失败：${(error as Error).message}`);
    }
  }

  async function saveTitle(id: string) {
    const next = draftTitle.trim();
    setEditingId(null);
    if (!next) return;
    try {
      await api.renameConversation(id, next);
      await loadConversations();
      setToast("已重命名");
    } catch (error) {
      setToast(`重命名失败：${(error as Error).message}`);
    }
  }

  async function removeConversation(id: string) {
    try {
      await api.removeConversation(id);
      if (conversationId === id) {
        setConversationId(null);
        setHasMessages(false);
      }
      await loadConversations();
      setToast("已删除会话");
    } catch (error) {
      setToast(`删除失败：${(error as Error).message}`);
    }
  }

  return (
    <div className={railOpen ? "app" : "app rail-collapsed"}>
      <aside className={railOpen ? "rail" : "rail collapsed"}>
        <div className={railOpen ? "rail-head" : "rail-head slim"}>
          {railOpen ? (
            <>
              <LogoMark />
              <span className="brand-name">个人知识库</span>
              <div className="rail-actions">
                <button className="icon-chip" title="搜索（Ctrl/⌘ + K）" onClick={() => setSearchOpen(true)}>
                  <Search size={17} />
                </button>
                <button className="icon-chip" title="关闭边栏" onClick={() => setRailOpen(false)}>
                  <PanelLeftClose size={17} />
                </button>
              </div>
            </>
          ) : (
            <button
              className="logo-switch"
              onMouseEnter={() => setLogoHover(true)}
              onMouseLeave={() => setLogoHover(false)}
              onClick={() => setRailOpen(true)}
            >
              {logoHover ? <PanelLeftOpen size={20} /> : <LogoMark />}
            </button>
          )}
        </div>

        {railOpen && (
          <button className="btn-new" onClick={() => void newChat()}>
            <PenLine size={16} />
            <span>发起新会话</span>
          </button>
        )}

        <nav className="nav">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={view === item.key ? "nav-item on" : "nav-item"}
                onClick={() => setView(item.key)}
              >
                <Icon size={17} className="nav-icon" />
                <span>{item.label}</span>

              </button>
            );
          })}
        </nav>

        {railOpen && (
          <>
            <div className="rail-label">全部</div>
            <div className="conv-scroll">
              {!visible.length && <p className="conv-empty">还没有会话</p>}
              {groups.map((group) => (
                <div className="conv-group" key={group.label}>
                  <div className="conv-group-label">{group.label}</div>
                  {group.items.map((item) => (
                    <div
                      key={item.id}
                      className={conversationId === item.id ? "conv on" : "conv"}
                      onClick={() => {
                        setConversationId(item.id);
                        setView("chat");
                      }}
                      title={item.title}
                    >
                      <span className="conv-title">{item.title}</span>
                      <button
                        className="conv-del"
                        title="删除会话"
                        onClick={(event) => {
                          event.stopPropagation();
                          void removeConversation(item.id);
                        }}
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
              ))}
            </div>

            <div className="rail-foot">
              <div>
                <span className={health?.database.ok ? "dot ok" : "dot bad"} />
                数据库 {health?.database.ok ? "正常" : "异常"}
              </div>
              <div>
                <span className={health?.llm.ok ? "dot ok" : "dot bad"} />
                模型 {health?.llm.ok ? "已连接" : "未配置"}
              </div>
            </div>
          </>
        )}
      </aside>

      <main className="main">
        {view === "chat" && (
          <ChatView
            conversationId={conversationId}
            onConversation={setConversationId}
            onRefreshConversations={loadConversations}
            onHasMessages={setHasMessages}
            documents={readyDocuments.map((item) => ({ id: item.id, original_name: item.original_name }))}
            panelOpen={panelOpen}
            onPanelOpenChange={setPanelOpen}
          />
        )}
        {view === "kb" && <KbView documents={documents} counts={counts} reload={loadDocuments} />}
        {view === "graph" && <GraphView documents={readyDocuments} onOpenSource={setSource} />}
      </main>

      <SearchModal
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        onOpenConversation={(id) => {
          setConversationId(id);
          setView("chat");
        }}
      />

      <SourceReader source={source} onClose={() => setSource(null)} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
