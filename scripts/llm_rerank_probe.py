"""LLM 精排可行性探针：不下载 1.1GB 重排模型，先用 deepseek-flash 验证「重排这一步有没有用」。

做法：每个问题召回 top-20，把「序号 + 文件名 + 页码 + 首句」交给模型重排，
对比「重排后的 top-5」与「原始 top-5」的命中率。

判断标准（写在报告里）：
- LLM 精排把命中率从 1/5 拉到 3/5 以上 → 重排有效，bge-reranker 值得下；
- 拉不动 → 瓶颈在召回，候选里根本没有正确片段，下 reranker 也没用。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.integrations import llm  # noqa: E402
from app.services.retrieval import RetrievedChunk  # noqa: E402
from app.services import retrieval  # noqa: E402

# 问题 + 判定命中的关键词（必须同时出现在同一片段里）
CASES: list[tuple[str, list[str]]] = [
    ("解释Agent含义", ["由role", "backstory"]),
    ("KTO 和 DPO 的区别", ["KTO", "DPO"]),
    ("checkpointer 有什么用", ["checkpointer"]),
    ("增量更新时为什么要先删旧向量", ["增量更新"]),
    ("多 Agent 系统为什么需要冗余", ["冗余"]),
]

SYSTEM = (
    "你是检索排序器。用户会给一个问题，以及候选片段（编号、文件名、页码、首句）。"
    "请只按「能否帮助回答问题」排序，输出 JSON：{\"order\": [编号, ...]}，"
    "包含所有候选编号，最相关的在前。不要解释。"
)


def _first_sentence(text: str, limit: int = 160) -> str:
    """取切片开头若干字。只给首句时，模型会误判「首句不相关」的切片，所以放宽到 limit 字。"""
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:limit]


def _locate(hits: list[RetrievedChunk], keywords: list[str]) -> int:
    """返回第一个包含全部关键词的候选序号（1 起），没有则 0。"""
    for index, hit in enumerate(hits, start=1):
        if all(keyword in hit.content for keyword in keywords):
            return index
    return 0


def _ask_llm(question: str, hits: list[RetrievedChunk], chars: int = 300) -> list[int]:
    lines = []
    for index, hit in enumerate(hits, start=1):
        name = Path(str(hit.filename or "?")).name
        lines.append(f"{index}. {name} {hit.location} | {_first_sentence(hit.content, chars)}")
    prompt = f"问题：{question}\n\n候选：\n" + "\n".join(lines)
    raw = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=300,
        json_mode=True,
    )
    try:
        order = json.loads(raw).get("order", [])
    except Exception:
        nums = re.findall(r"\d+", raw)
        order = [int(n) for n in nums]
    seen, cleaned = set(), []
    for item in order:
        if isinstance(item, int) and 1 <= item <= len(hits) and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=int, default=20, help="召回候选数")
    parser.add_argument("--top", type=int, default=5, help="取前几作为上下文")
    parser.add_argument("--chars", type=int, default=300, help="候选展示给模型的字数")
    args = parser.parse_args()

    base_hits = llm_hits = 0
    rows = []

    for question, keywords in CASES:
        pool = retrieval.search_raw(retrieval.rewrite_query(question), top_k=args.pool)
        original_rank = _locate(pool, keywords)

        order = _ask_llm(question, pool, args.chars)
        reranked = [pool[i - 1] for i in order] + [hit for i, hit in enumerate(pool, 1) if i not in order]
        rerank_rank = _locate(reranked, keywords)

        base_ok = 0 < original_rank <= args.top
        rerank_ok = 0 < rerank_rank <= args.top
        base_hits += base_ok
        llm_hits += rerank_ok
        rows.append(
            {
                "question": question,
                "original_rank": original_rank,
                "rerank_rank": rerank_rank,
                "base_hit": base_ok,
                "rerank_hit": rerank_ok,
            }
        )
        print(
            f"{'✓' if rerank_ok else '✗'} {question[:24]:26} "
            f"原排名={original_rank or '-':>2} → 精排后={rerank_rank or '-':>2}",
            flush=True,
        )

    total = len(CASES)
    print(f"\n命中率 top{args.top}：原始 {base_hits}/{total} → LLM 精排 {llm_hits}/{total}")
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
