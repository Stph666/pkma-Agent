import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Detail, DocumentItem } from "../types";

type Props = {
  documents: DocumentItem[];
  counts: Record<string, number>;
  reload: () => Promise<void>;
};

const FILTERS = ["全部", "就绪", "处理中", "失败"] as const;
const STATUS_VALUE: Record<string, string> = { 就绪: "ready", 处理中: "processing", 失败: "failed" };

export default function KbView({ documents, counts, reload }: Props) {
  const [filter, setFilter] = useState<string>("全部");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  // 超过建议字数时暂存文件，等用户确认后带 force 重传
  const [pending, setPending] = useState<{ file: File; message: string } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const list = useMemo(
    () => documents.filter((item) => (filter === "全部" ? true : item.status === STATUS_VALUE[filter])),
    [documents, filter],
  );

  useEffect(() => {
    if (list.length && !list.some((item) => item.id === selected)) {
      setSelected(list[0].id);
    }
  }, [list, selected]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api.document(selected).then(setDetail).catch(() => setDetail(null));
  }, [selected, documents]);

  async function upload(files: FileList | null, force = false) {
    const queue = files ? Array.from(files) : pending ? [pending.file] : [];
    if (!queue.length) return;
    setBusy(true);
    setMessage("");
    setPending(null);
    const notes: string[] = [];
    for (const file of queue) {
      try {
        const { warning } = await api.upload(file, overwrite, force);
        notes.push(`《${file.name}》已接收${warning ? `（${warning}）` : ""}`);
      } catch (error) {
        const err = error as Error & { code?: string };
        if (err.code === "document_large_confirm") {
          setPending({ file, message: err.message });
          notes.push(`《${file.name}》${err.message}`);
        } else {
          notes.push(`《${file.name}》未入库：${err.message}`);
        }
      }
    }
    setMessage(notes.join("；"));
    await reload();
    setBusy(false);
  }

  async function act(kind: "detail" | "enrich" | "reindex" | "delete") {
    if (!selected) return;
    setBusy(true);
    try {
      if (kind === "detail") {
        setDetail(await api.document(selected));
      } else if (kind === "enrich") {
        await api.enrich(selected, "standard");
        setMessage("已开始生成摘要与图谱");
      } else if (kind === "reindex") {
        await api.reindex(selected);
        setMessage("已开始重新索引");
      } else {
        await api.removeDocument(selected);
        setMessage("已删除");
        setSelected("");
        setDetail(null);
      }
      await reload();
    } catch (error) {
      setMessage(`操作失败：${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>知识库</h1>
          <p>支持 PDF / DOCX / TXT，上传后自动解析、切片并建立向量索引。</p>
        </div>
        <button className="btn-secondary" onClick={() => void reload()}>
          刷新列表
        </button>
      </header>

      <div
        className="dropzone"
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDrop={(event) => {
          event.preventDefault();
          // 文案写着「拖到这里」，之前并没有真正处理拖拽
          void upload(event.dataTransfer.files);
        }}
      >
        <div className="dz-icon">↑</div>
        <div className="dz-text">
          <h3>上传文档</h3>
          <p>拖到这里，或点击选择文件 · 同名文档可选择覆盖</p>
        </div>
        <label className="check">
          <input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />
          覆盖同名
        </label>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          accept=".pdf,.docx,.txt"
          onChange={(event) => void upload(event.target.files)}
        />
        <button className="btn-primary" disabled={busy} onClick={() => fileRef.current?.click()}>
          {busy ? "处理中…" : "选择文件"}
        </button>
      </div>

      {message && <p className="notice">{message}</p>}

      {pending && (
        <div className="confirm-row">
          <span>{pending.message}</span>
          <button className="btn-secondary" disabled={busy} onClick={() => void upload(null, true)}>
            强制继续
          </button>
          <button className="link" onClick={() => setPending(null)}>
            取消
          </button>
        </div>
      )}

      <div className="kb-grid">
        <section className="card table-card">
          <div className="table-head">
            <h3>
              文档列表 <span className="dim">共 {documents.length} 份</span>
            </h3>
            <div className="filters">
              {FILTERS.map((item) => (
                <button
                  key={item}
                  className={filter === item ? "filter on" : "filter"}
                  onClick={() => setFilter(item)}
                >
                  {item}
                  {item !== "全部" && counts[STATUS_VALUE[item]] ? ` ${counts[STATUS_VALUE[item]]}` : ""}
                </button>
              ))}
            </div>
          </div>

          <table className="doc-table">
            <thead>
              <tr>
                <th>文件名</th>
                <th>大小</th>
                <th>状态</th>
                <th>分类</th>
                <th>标签</th>
                <th>切片</th>
              </tr>
            </thead>
            <tbody>
              {list.map((item) => (
                <tr
                  key={item.id}
                  className={item.id === selected ? "row on" : "row"}
                  onClick={() => setSelected(item.id)}
                >
                  <td className="fname">
                    <span className={`ftype ${item.extension.replace(".", "")}`}>
                      {item.extension.replace(".", "").slice(0, 3).toUpperCase()}
                    </span>
                    {item.original_name}
                  </td>
                  <td className="num">{formatSize(item.size)}</td>
                  <td>
                    <span className={`state ${item.status}`}>{label(item.status)}</span>
                  </td>
                  <td className="dim">{item.category || "—"}</td>
                  <td>
                    {item.tags.slice(0, 3).map((tag) => (
                      <span className="tagp" key={tag}>
                        {tag}
                      </span>
                    ))}
                    {item.tags.length > 3 && <span className="tagp dim">+{item.tags.length - 3}</span>}
                  </td>
                  <td className="num">{item.chunk_count}</td>
                </tr>
              ))}
              {!list.length && (
                <tr>
                  <td colSpan={6} className="empty-row">
                    这个筛选下还没有文档。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        <aside className="card detail-card">
          {detail ? (
            <>
              <h3>{detail.original_name}</h3>
              <div className="kv">
                <div>
                  <span>状态</span>
                  <b className={`state ${detail.status}`}>{label(detail.status)}</b>
                </div>
                <div>
                  <span>知识组织</span>
                  <b>{enrichLabel(detail.enrichment_status)}</b>
                </div>
                <div>
                  <span>切片</span>
                  <b>{detail.chunk_count} 片</b>
                </div>
                <div>
                  <span>分类 / 标签</span>
                  <b>
                    {detail.category || "—"}
                    {detail.tags.length ? ` · ${detail.tags.slice(0, 3).join("、")}` : ""}
                  </b>
                </div>
              </div>
              <div className="summary-box">{detail.summary || "还没有摘要，可点「生成摘要与图谱」。"}</div>
              <div className="detail-actions">
                <button className="btn-secondary" disabled={busy} onClick={() => void act("enrich")}>
                  生成摘要与图谱
                </button>
                <button className="btn-secondary" disabled={busy} onClick={() => void act("reindex")}>
                  重新索引
                </button>
                {confirmDelete ? (
                  <>
                    <button
                      className="btn-danger"
                      disabled={busy}
                      onClick={() => {
                        setConfirmDelete(false);
                        void act("delete");
                      }}
                    >
                      确认删除（含索引与图谱）
                    </button>
                    <button className="btn-secondary" onClick={() => setConfirmDelete(false)}>
                      取消
                    </button>
                  </>
                ) : (
                  <button className="btn-danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                    删除
                  </button>
                )}
              </div>
            </>
          ) : (
            <p className="sources-note">选择左侧文档查看摘要、标签与切片。</p>
          )}
        </aside>
      </div>
    </div>
  );
}

function label(status: string) {
  return status === "ready" ? "就绪" : status === "processing" ? "处理中" : status === "failed" ? "失败" : status;
}

function enrichLabel(status: string) {
  if (status === "done") return "已完成";
  if (status === "failed") return "失败（图谱可能过期）";
  if (status === "processing") return "生成中";
  return "待生成";
}

function formatSize(size: number) {
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size > 1024) return `${Math.round(size / 1024)} KB`;
  return `${size} B`;
}
