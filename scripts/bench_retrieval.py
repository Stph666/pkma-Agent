"""检索质量基准：固定问题集，输出命中排名、分数与档位。

用法：python scripts/bench_retrieval.py [--top 5]

每个用例给出「必须出现的关键词」，报告第一个同时包含这些关键词的切片排名。
排名 1–5 记为命中，6+ 记为未命中；档位反映会不会拒答。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import retrieval  # noqa: E402

CASES: list[tuple[str, list[str]]] = [
    ("解释Agent含义", ["由role", "backstory"]),
    ("KTO 和 DPO 的区别", ["KTO", "DPO"]),
    ("checkpointer 有什么用", ["checkpointer"]),
    ("知识图谱的三元组是什么", ["三元组"]),
    ("增量更新时为什么要先删旧向量", ["增量更新"]),
    ("LangGraph 和 AutoGen 怎么选", ["AutoGen"]),
    ("多 Agent 系统为什么需要冗余", ["冗余"]),
    ("Transformer 的自注意力怎么计算", ["注意力"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--rewrite", action="store_true", help="启用问题改写")
    args = parser.parse_args()

    hits = 0
    tiers: dict[str, int] = {}
    rows: list[str] = []

    for question, keywords in CASES:
        query = retrieval.rewrite_query(question) if args.rewrite else question
        result = retrieval.search(query)
        tiers[result.tier] = tiers.get(result.tier, 0) + 1

        rank = 0
        for index, chunk in enumerate(result.chunks, start=1):
            if all(keyword in chunk.content for keyword in keywords):
                rank = index
                break

        ok = 0 < rank <= args.top
        hits += 1 if ok else 0
        top = result.chunks[0] if result.chunks else None
        rows.append(
            f"{'✓' if ok else '✗'} {question[:22]:24} "
            f"目标排名={rank or '-':>2} tier={result.tier:<7} "
            f"top1={top.score:.3f} {top.location if top else '-'}"
        )

    print("\n".join(rows))
    print(
        f"\n命中率 {hits}/{len(CASES)}（目标排名 ≤{args.top}）"
        f" | 档位分布 {tiers}"
    )
    return 0 if hits == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
