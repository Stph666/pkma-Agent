import type { Conversation, Detail, DocumentItem, Health, MessageOutLike } from "./types";

const BASE = "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!response.ok) {
    let message = `${response.status}`;
    let code = "";
    try {
      const payload = await response.json();
      message = payload.message || payload.detail?.message || message;
      code = payload.code || "";
    } catch {
      /* 忽略解析失败 */
    }
    const error = new Error(message) as Error & { code?: string };
    error.code = code;
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export type SearchHit = {
  id: string;
  conversation_id: string;
  title: string;
  role: string;
  snippet: string;
  created_at: string;
};

export const api = {
  search: (q: string) =>
    request<{
      query: string;
      conversations: { id: string; title: string; updated_at: string }[];
      messages: SearchHit[];
    }>(`/api/search?q=${encodeURIComponent(q)}`),

  chunkContext: (chunkId: string, budget = 2600) =>
    request<{
      chunk_id: string;
      document_id: string;
      filename: string;
      location: string;
      text: string;
      highlight: string;
      chunks: number;
      page_start?: number | null;
    }>(`/api/chunks/${chunkId}/context?budget=${budget}`),

  suggestions: (n = 3) => request<{ items: string[] }>(`/api/suggestions?n=${n}`),

  health: () => request<Health>("/api/health"),

  documents: (status?: string) =>
    request<{ items: DocumentItem[]; total: number; counts: Record<string, number> }>(
      `/api/documents${status && status !== "全部" ? `?status=${encodeURIComponent(status)}` : ""}`,
    ),

  document: (id: string) => request<Detail>(`/api/documents/${id}`),

  upload: async (file: File, overwrite: boolean, force = false) => {
    const form = new FormData();
    form.append("file", file);
    form.append("overwrite", String(overwrite));
    form.append("force", String(force));
    const response = await fetch("/api/documents", { method: "POST", body: form });
    if (!response.ok) {
      let message = `${response.status}`;
      let code = "";
      try {
        const payload = await response.json();
        message = payload.message || message;
        code = payload.code || "";
      } catch {
        /* 忽略 */
      }
      const error = new Error(message) as Error & { code?: string };
      error.code = code;
      throw error;
    }
    const rawWarning = response.headers.get("X-Ingest-Warning") || "";
    let warning = "";
    try {
      warning = rawWarning ? decodeURIComponent(rawWarning) : "";
    } catch {
      warning = rawWarning;
    }
    return { item: (await response.json()) as DocumentItem, warning };
  },

  retryFailed: () => request<{ retried: number }>("/api/documents/retry-failed", { method: "POST" }),

  reindex: (id: string) => request<DocumentItem>(`/api/documents/${id}/reindex`, { method: "POST" }),

  enrich: (id: string, depth: string) =>
    request<DocumentItem>(`/api/documents/${id}/enrich?depth=${encodeURIComponent(depth)}`, { method: "POST" }),

  removeDocument: (id: string) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),

  conversations: () => request<{ items: Conversation[]; total: number }>("/api/conversations"),

  createConversation: () =>
    request<Conversation>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: null, mode: "knowledge", document_ids: [] }),
    }),

  messages: (id: string) => request<MessageOutLike[]>(`/api/conversations/${id}/messages`),

  renameConversation: (id: string, title: string) =>
    request<{ id: string; title: string }>(`/api/conversations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),

  removeConversation: (id: string) => request<void>(`/api/conversations/${id}`, { method: "DELETE" }),

  graphExploreData: (params: URLSearchParams) =>
    request<{
      nodes: { data: any }[];
      edges: { data: any }[];
      stats: { entities: number; relations: number; cooccur: number; alias: number; avg_degree: number };
    }>(`/api/graph/explore?${params.toString()}`),

  graphExplore: (params: URLSearchParams) =>
    request<{ html: string; stats: { entities: number; relations: number; cooccur: number; avg_degree: number } }>(
      `/api/graph/explore/html?${params.toString()}`,
    ),

  graphMindmap: (params: URLSearchParams) =>
    request<{ html: string; entities: number }>(`/api/graph/mindmap?${params.toString()}`),

  graphHtml: (params: URLSearchParams) => request<{ html: string; shown_entities: number; total_entities: number; shown_relations: number; total_relations: number }>(`/api/graph/html?${params.toString()}`),
};

export type StreamEvent =
  | { type: "meta"; conversation_id: string; retrieved: number; top_score: number }
  | { type: "delta"; text: string }
  | { type: "citations"; citations: import("./types").Citation[] }
  | { type: "done"; status: string; elapsed: number; cited: number[] }
  | { type: "error"; message: string };

export async function streamChat(
  body: {
    message: string;
    mode: string;
    document_ids: string[];
    conversation_id?: string | null;
    regenerate?: boolean;
  },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`请求失败：${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      let event = "message";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      try {
        const payload = JSON.parse(dataLines.join("\n"));
        onEvent({ type: event, ...payload } as StreamEvent);
      } catch {
        /* 忽略半包 */
      }
    }
  }
}
