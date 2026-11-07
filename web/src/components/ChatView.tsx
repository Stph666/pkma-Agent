import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AnimatePresence, motion } from "motion/react";
import {
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Copy,
  Maximize,
  Maximize2,
  Minimize2,
  PanelRight,
  Plus,
  RotateCcw,
  Sparkles,
  Square,
  X,
} from "lucide-react";
import { api, streamChat } from "../api";
import type { ChatMessage, Citation } from "../types";

type Props = {
  conversationId: string | null;
  onConversation: (id: string) => void;
  onRefreshConversations: () => void;
  documents: { id: string; original_name: string }[];
  panelOpen: boolean;
  onPanelOpenChange: (open: boolean) => void;
  onHasMessages: (value: boolean) => void;
};

type Phase = "idle" | "sending" | "streaming" | "stopping";

const SAMPLES = [
  "增量更新时为什么要先删旧向量？",
  "切片长度对召回有什么影响？",
  "知识图谱里的三元组是什么？",
];

const LONG_MESSAGE = 520;

/** 把命中的那一段整体标出来：读者要的是「这一节里哪段在回答」，不是零散高亮。 */
function highlightSection(text: string, needle?: string) {
  const probe = (needle || "").trim().slice(0, 60);
  if (!probe || probe.length < 6) return text;
  const index = text.indexOf(probe);
  if (index < 0) return text;
  const paragraphs: { start: number; end: number }[] = [];
  let cursor = 0;
  for (const piece of text.split("\n\n")) {
    paragraphs.push({ start: cursor, end: cursor + piece.length });
    cursor += piece.length + 2;
  }
  const hitBox = paragraphs.find((box) => index >= box.start && index <= box.end);
  if (!hitBox) return text;
  return (
    <>
      {text.slice(0, hitBox.start)}
      <mark className="para">{text.slice(hitBox.start, hitBox.end)}</mark>
      {text.slice(hitBox.end)}
    </>
  );
}

export default function ChatView({
  conversationId,
  onConversation,
  onRefreshConversations,
  documents,
  panelOpen,
  onPanelOpenChange,
  onHasMessages,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<"knowledge" | "general">("knowledge");
  const [scope, setScope] = useState<string>("");
  const [status, setStatus] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [activeCite, setActiveCite] = useState<number | null>(null);
  const [reader, setReader] = useState<{ index: number } | null>(null);
  const [section, setSection] = useState<{ text: string; location: string } | null>(null);
  // 打开就是宽栏（压缩对话），再点全屏直接把对话挤掉
  const [panelFull, setPanelFull] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [failure, setFailure] = useState<{ question: string; message: string } | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [samples, setSamples] = useState<string[]>(SAMPLES);
  // 点击进入输入状态后隐藏占位；CSS 的 :focus 在部分环境不触发，这里再兜一层
  const [typing, setTyping] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const skipLoadRef = useRef<string | null>(null);
  const autoOpenedRef = useRef(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const started = messages.length > 0;
  const busy = phase !== "idle";

  // 来源面板只跟「最后一条回答」：通用模式回答没有引用时必须清空，
  // 否则会沿用上一轮的旧来源，看起来像这轮也引用了原文。
  const latestCitations: Citation[] = useMemo(() => {
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant") return [];
    return last.citations ?? [];
  }, [messages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  useEffect(() => {
    onHasMessages(messages.length > 0);
  }, [messages.length, onHasMessages]);

  useEffect(() => {
    if (conversationId) return;
    api
      .suggestions(3)
      .then((data) => {
        if (data.items.length) setSamples(data.items);
      })
      .catch(() => setSamples(SAMPLES));
  }, [conversationId]);

  useEffect(() => {
    if (!reader) {
      setSection(null);
      return;
    }
    const citation = latestCitations[reader.index];
    if (!citation?.chunk_id) return;
    let alive = true;
    api
      .chunkContext(citation.chunk_id)
      .then((data) => {
        if (alive) setSection({ text: data.text, location: data.location });
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [reader, latestCitations]);

  useEffect(() => {
    if (!panelOpen) {
      setReader(null);
      setPanelFull(false);
    }
  }, [panelOpen]);

  useEffect(() => {
    if (!conversationId) {
      setMessages([]);
      return;
    }
    if (skipLoadRef.current === conversationId) {
      skipLoadRef.current = null;
      return;
    }
    let cancelled = false;
    setLoadingHistory(true);
    setFailure(null);
    api
      .messages(conversationId)
      .then((items) => {
        if (cancelled) return;
        setMessages(
          items
            .filter((item) => item.role !== "system")
            .map((item) => ({
              id: item.id,
              role: item.role as "user" | "assistant",
              content: item.content,
              status: item.status,
              citations: item.citations || [],
            })),
        );
      })
      .catch(() => setMessages([]))
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [menuOpen]);

  async function ensureConversation(): Promise<string> {
    if (conversationId) return conversationId;
    const created = await api.createConversation();
    skipLoadRef.current = created.id;
    onConversation(created.id);
    onRefreshConversations();
    return created.id;
  }

  async function send(text: string, regenerate = false) {
    const question = text.trim();
    if (!question || busy) return;

    setFailure(null);
    const cid = await ensureConversation();
    const assistantId = `stream-${Date.now()}`;

    if (!regenerate) {
      setMessages((prev) => [
        ...prev,
        { id: `user-${Date.now()}`, role: "user", content: question },
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);
    } else {
      setMessages((prev) => {
        const trimmed = [...prev];
        while (trimmed.length && trimmed[trimmed.length - 1].role === "assistant") trimmed.pop();
        return [...trimmed, { id: assistantId, role: "assistant", content: "", streaming: true }];
      });
    }

    setInput("");
    setStatus(mode === "knowledge" ? "检索中…" : "生成中…");
    setPhase("sending");
    setActiveCite(null);
    setReader(null);

    const controller = new AbortController();
    abortRef.current = controller;
    let buffer = "";
    let gotFirstDelta = false;

    try {
      await streamChat(
        {
          message: question,
          mode,
          document_ids: scope ? [scope] : [],
          conversation_id: cid,
          regenerate,
        },
        (event) => {
          if (event.type === "delta") {
            if (!gotFirstDelta) {
              gotFirstDelta = true;
              setPhase("streaming");
            }
            buffer += event.text;
            setMessages((prev) =>
              prev.map((item) => (item.id === assistantId ? { ...item, content: buffer } : item)),
            );
          } else if (event.type === "citations") {
            setMessages((prev) =>
              prev.map((item) =>
                item.id === assistantId ? { ...item, citations: event.citations } : item,
              ),
            );
            if (event.citations.length && !panelOpen && !autoOpenedRef.current) {
              autoOpenedRef.current = true;
              onPanelOpenChange(true);
            }
          } else if (event.type === "done") {
            const label =
              event.status === "no_evidence"
                ? "知识库中没有足够依据"
                : event.status === "partial"
                  ? "证据相关度一般，仅部分回答"
                  : `完成 · ${event.elapsed}s`;
            setStatus(label);
            setMessages((prev) =>
              prev.map((item) =>
                item.id === assistantId
                  ? { ...item, streaming: false, status: event.status, content: buffer || "（无输出）" }
                  : item,
              ),
            );
          } else if (event.type === "error") {
            setStatus("生成失败");
            setFailure({ question, message: event.message });
            setMessages((prev) =>
              prev.map((item) =>
                item.id === assistantId
                  ? { ...item, streaming: false, content: `生成失败：${event.message}` }
                  : item,
              ),
            );
          }
        },
        controller.signal,
      );
      onRefreshConversations();
    } catch (error) {
      const aborted = (error as Error).name === "AbortError";
      if (aborted) {
        setStatus("已停止生成");
      } else {
        setStatus("请求失败");
        setFailure({ question, message: (error as Error).message });
      }
      setMessages((prev) =>
        prev.map((item) => (item.id === assistantId ? { ...item, streaming: false } : item)),
      );
    } finally {
      setPhase("idle");
      abortRef.current = null;
      inputRef.current?.focus();
    }
  }

  function stop() {
    if (!abortRef.current) return;
    setPhase("stopping");
    setStatus("停止中…");
    abortRef.current.abort();
  }

  const scopeLabel = scope
    ? documents.find((item) => item.id === scope)?.original_name || "指定文档"
    : "全部文档";

  const composer = (
    <div className="composer-wrap">
      <div className="mode-row">
        <div className="seg">
          <button
            className={mode === "knowledge" ? "seg-item on" : "seg-item"}
            onClick={() => setMode("knowledge")}
          >
            知识库
          </button>
          <button
            className={mode === "general" ? "seg-item on" : "seg-item"}
            onClick={() => setMode("general")}
          >
            通用对话
          </button>
        </div>
      </div>

      <div className="composer">
        <div className="plus-wrap" ref={menuRef}>
          <button className="plus" title="选择文档范围" onClick={() => setMenuOpen((value) => !value)}>
            <Plus size={16} />
          </button>
          <AnimatePresence>
            {menuOpen && (
              <motion.div
                className="plus-menu"
                initial={{ opacity: 0, y: 6, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 6, scale: 0.98 }}
                transition={{ duration: 0.16, ease: "easeOut" }}
              >
                <div className="plus-title">文档范围</div>
                <button
                  className={scope === "" ? "plus-item on" : "plus-item"}
                  onClick={() => {
                    setScope("");
                    setMenuOpen(false);
                  }}
                >
                  全部文档
                </button>
                {documents.map((item) => (
                  <button
                    key={item.id}
                    className={scope === item.id ? "plus-item on" : "plus-item"}
                    title={item.original_name}
                    onClick={() => {
                      setScope(item.id);
                      setMenuOpen(false);
                    }}
                  >
                    {item.original_name}
                  </button>
                ))}
                {!documents.length && <p className="plus-empty">还没有可用文档</p>}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <textarea
          ref={inputRef}
          value={input}
          rows={1}
          placeholder={typing ? "" : mode === "knowledge" ? "向知识库提问…" : "问点什么…"}
          onClick={() => setTyping(true)}
          onFocus={() => setTyping(true)}
          onBlur={() => setTyping(false)}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send(input);
            }
          }}
        />

        {scope && (
          <button className="scope-tag" title="清除文档范围" onClick={() => setScope("")}>
            {scopeLabel} <X size={11} />
          </button>
        )}

        {busy ? (
          <button className="btn-stop" onClick={stop} disabled={phase === "stopping"} title="停止">
            {phase === "stopping" ? <span className="spinner" /> : <Square size={13} />}
          </button>
        ) : (
          <button
            className={input.trim() ? "btn-send" : "btn-send idle"}
            onClick={() => void send(input)}
            disabled={!input.trim()}
            title="发送"
          >
            <ArrowUp size={17} />
          </button>
        )}
      </div>

      {failure && (
        <div className="composer-hint">
          <span className="hint-error">请求失败：{failure.message}</span>
          <button className="hint-retry" onClick={() => void send(failure.question, true)}>
            重试
          </button>
        </div>
      )}
    </div>
  );

  return (
    <div
      className={
        panelOpen ? `chat-layout with-panel${panelFull ? " panel-full" : ""}` : "chat-layout"
      }
    >
      {!panelOpen && (
        <button className="panel-tab" title="展开来源" onClick={() => onPanelOpenChange(true)}>
          <PanelRight size={16} />
        </button>
      )}

      <section className="chat-main">


        {loadingHistory ? (
          <div className="thread">
            <div className="thread-inner">
              <div className="skeleton-msg" />
              <div className="skeleton-msg short" />
              <div className="skeleton-msg" />
            </div>
          </div>
        ) : started ? (
          <div className="thread">
            <div className="thread-inner">
              <AnimatePresence initial={false}>
                {messages.map((message) =>
                  message.role === "user" ? (
                    <motion.div
                      className="row-user"
                      key={message.id}
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.18, ease: "easeOut" }}
                    >
                      <div className="bubble-user">
                        {message.content.length > LONG_MESSAGE && !expanded[message.id]
                          ? `${message.content.slice(0, LONG_MESSAGE)}…`
                          : message.content}
                        {message.content.length > LONG_MESSAGE && (
                          <button
                            className="expand-btn"
                            onClick={() =>
                              setExpanded((prev) => ({ ...prev, [message.id]: !prev[message.id] }))
                            }
                          >
                            {expanded[message.id] ? "收起" : "展开全文"}
                          </button>
                        )}
                      </div>
                    </motion.div>
                  ) : (
                    <motion.article
                      className="answer"
                      key={message.id}
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.18, ease: "easeOut" }}
                    >
                      <div className="answer-body">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            p: ({ children }) => <p>{decorate(children, setActiveCite)}</p>,
                            li: ({ children }) => <li>{decorate(children, setActiveCite)}</li>,
                          }}
                        >
                          {message.content}
                        </ReactMarkdown>
                        {message.streaming && <span className="caret" />}
                      </div>
                      {!message.streaming && message.status === "partial" && (
                        <div className="answer-note">
                          检索到的片段相关度一般，以上仅就有依据的部分作答。
                        </div>
                      )}
                      {!message.streaming && (
                        <div className="answer-actions">
                          <button
                            className="icon-btn"
                            title="复制"
                            onClick={() => {
                              void navigator.clipboard.writeText(message.content);
                              setStatus("已复制");
                            }}
                          >
                            <Copy size={13} />
                          </button>
                          <button
                            className="icon-btn"
                            title="重新生成"
                            onClick={() => void send(lastUser(messages), true)}
                          >
                            <RotateCcw size={13} />
                          </button>
                        </div>
                      )}
                    </motion.article>
                  ),
                )}
              </AnimatePresence>
              <div ref={bottomRef} />
            </div>
          </div>
        ) : (
          <div className="landing">
            <motion.h1
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.22, ease: "easeOut" }}
            >
              有什么我能帮你的吗？
            </motion.h1>
            {composer}
            <motion.div
              className="landing-samples"
              initial="hidden"
              animate="show"
              variants={{ hidden: {}, show: { transition: { staggerChildren: 0.04, delayChildren: 0.08 } } }}
            >
              {samples.map((sample) => (
                <motion.button
                  key={sample}
                  className="sample"
                  variants={{ hidden: { opacity: 0, y: 6 }, show: { opacity: 1, y: 0 } }}
                  transition={{ duration: 0.18, ease: "easeOut" }}
                  onClick={() => {
                    setInput(sample);
                    inputRef.current?.focus();
                  }}
                >
                  <Sparkles size={14} className="sample-icon" />
                  {sample}
                </motion.button>
              ))}
            </motion.div>
          </div>
        )}

        {started && !loadingHistory && composer}
      </section>

      <AnimatePresence>
        {panelOpen && (
          <motion.aside
            className={panelFull ? "sources full" : "sources"}
            initial={{ opacity: 0, x: 16 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 16 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
          >
            <div className="panel-head">
              <span className="panel-title">来源 · {latestCitations.length}</span>
              <button
                className="head-btn"
                title={panelFull ? "退出全屏" : "全屏阅读"}
                onClick={() => setPanelFull((value) => !value)}
              >
                {panelFull ? <Minimize2 size={14} /> : <Maximize size={14} />}
              </button>
              <button className="head-btn" title="收起来源" onClick={() => onPanelOpenChange(false)}>
                <X size={15} />
              </button>
            </div>

            {reader && latestCitations[reader.index] ? (
              <div className="reader">
                <div className="reader-head">
                  <button className="ghost" onClick={() => setReader(null)}>
                    <ChevronLeft size={12} /> 来源
                  </button>
                  <span className="reader-title">{latestCitations[reader.index].filename}</span>
                  <span className="reader-page">{section?.location || latestCitations[reader.index].location}</span>
                </div>
                <div className="reader-body">
                  {highlightSection(
                    section?.text || latestCitations[reader.index].content || latestCitations[reader.index].snippet,
                    latestCitations[reader.index].quote || latestCitations[reader.index].snippet.slice(0, 40),
                  )}
                </div>
                <div className="reader-foot">
                  <button
                    className="ghost"
                    disabled={reader.index <= 0}
                    onClick={() => setReader({ index: reader.index - 1 })}
                  >
                    上一条
                  </button>
                  <span>
                    {reader.index + 1} / {latestCitations.length}
                  </span>
                  <button
                    className="ghost"
                    disabled={reader.index >= latestCitations.length - 1}
                    onClick={() => setReader({ index: reader.index + 1 })}
                  >
                    下一条 <ChevronRight size={12} />
                  </button>
                </div>
              </div>
            ) : (
              <div className="src-list">
                {latestCitations.length === 0 && (
                  <p className="sources-note">
                    本轮回答没有可追溯的原文片段。可能是问题超出知识库范围，或命中的片段都低于阈值。
                  </p>
                )}
                {latestCitations.map((item, index) => (
                  <article
                    key={`${item.chunk_id}-${item.index}`}
                    className={activeCite === item.index ? "src lit" : "src"}
                    onClick={() => setActiveCite(activeCite === item.index ? null : item.index)}
                  >
                    <div className="src-top">
                      <span className="src-idx">{item.index}</span>
                      <span className="src-name">{item.filename}</span>
                      <span className="src-page">{item.location}</span>
                    </div>
                    <p className="src-text">{item.snippet}</p>
                    {item.supports && (
                      <p className="src-supports" title={item.supports}>
                        对应答案：{item.supports}
                      </p>
                    )}
                    {item.quote && <p className="src-quote">“{item.quote}”</p>}
                    <div className="src-foot">
                      <span className="src-score">
                        {item.origin === "graph"
                          ? "图谱补充"
                          : item.origin === "compare"
                            ? "对比补充"
                            : `相似度 ${item.score.toFixed(2)}`}
                      </span>
                      <button
                        className="src-open"
                        onClick={(event) => {
                          event.stopPropagation();
                          setReader({ index });
                          setPanelFull(true);
                        }}
                      >
                        查看原文 →
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
}

function lastUser(messages: ChatMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") return messages[i].content;
  }
  return "";
}

/** 把 [1] 变成可点击角标。 */
function decorate(children: React.ReactNode, onClick: (index: number) => void): React.ReactNode {
  return Array.isArray(children)
    ? children.map((child, index) => <span key={index}>{decorate(child, onClick)}</span>)
    : typeof children === "string"
      ? children.split(/(\[\d{1,2}\])/g).map((piece, index) => {
          const match = piece.match(/^\[(\d{1,2})\]$/);
          if (!match) return <span key={index}>{piece}</span>;
          return (
            <button key={index} className="cite" onClick={() => onClick(Number(match[1]))}>
              {match[1]}
            </button>
          );
        })
      : children;
}
