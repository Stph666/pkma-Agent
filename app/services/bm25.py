"""BM25 关键词打分（字符二元组），用于和向量分数融合。

不引入 jieba 之类的分词依赖：中文用字符二元组已能覆盖专有名词、缩写、编号，
而且对「KTO」「DPO」「checkpointer」这类词更稳。
"""

from __future__ import annotations

import math


def tokenize(text: str) -> list[str]:
    cleaned = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    return [cleaned[i : i + 2] for i in range(max(0, len(cleaned) - 1))]


def bigrams(text: str) -> list[str]:
    """兼容旧调用名。"""
    return tokenize(text)


def score(query: str, documents: list[str], k1: float = 1.4, b: float = 0.75) -> list[float]:
    """返回每个文档的 BM25 分数（在给定候选集内计算 idf）。"""
    query_terms = tokenize(query)
    if not query_terms or not documents:
        return [0.0] * len(documents)

    tokenized = [tokenize(doc) for doc in documents]
    lengths = [len(tokens) for tokens in tokenized]
    avg_length = sum(lengths) / max(1, len(lengths))

    doc_freq: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    total = len(documents)

    scores: list[float] = []
    for tokens, length in zip(tokenized, lengths):
        counts: dict[str, int] = {}
        for term in tokens:
            counts[term] = counts.get(term, 0) + 1
        value = 0.0
        for term in query_terms:
            if term not in counts:
                continue
            idf = math.log(1 + (total - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            tf = counts[term]
            value += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / max(1.0, avg_length)))
        scores.append(value)
    return scores


def normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def fuse(vector_scores: list[float], keyword_scores: list[float], keyword_weight: float) -> list[float]:
    """按权重融合两路归一化分数。keyword_weight=0.3 表示 0.7 向量 + 0.3 关键词。"""
    left = normalize(vector_scores)
    right = normalize(keyword_scores)
    return [
        (1 - keyword_weight) * vec + keyword_weight * key
        for vec, key in zip(left, right)
    ]
