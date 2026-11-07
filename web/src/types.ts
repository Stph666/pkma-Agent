export type DocumentItem = {
  id: string;
  original_name: string;
  extension: string;
  size: number;
  status: "processing" | "ready" | "failed";
  error_message?: string | null;
  summary?: string | null;
  category?: string | null;
  tags: string[];
  enrichment_status: string;
  enrichment_error?: string | null;
  chunk_count: number;
  created_at: string;
};

export type Chunk = {
  id: string;
  chunk_index: number;
  page_start?: number | null;
  page_end?: number | null;
  content: string;
};

export type Detail = DocumentItem & { chunks: Chunk[] };

export type Conversation = {
  id: string;
  title: string;
  message_count?: number;
  mode: string;
  document_ids: string[];
  updated_at: string;
};

export type Citation = {
  index: number;
  chunk_id: string;
  document_id: string;
  filename: string;
  location: string;
  snippet: string;
  content?: string;
  score: number;
  origin?: string;
  quote?: string;
  supports?: string;
  quote_score?: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status?: string;
  citations?: Citation[];
  streaming?: boolean;
};

export type Health = {
  database: { ok: boolean; detail: string };
  llm: { ok: boolean; model?: string; detail?: string };
  embedding?: { ok: boolean; model?: string };
};

export type MessageOutLike = {
  id: string;
  role: string;
  content: string;
  mode: string;
  status: string;
  citations: Citation[];
  error_message?: string | null;
  created_at: string;
};
