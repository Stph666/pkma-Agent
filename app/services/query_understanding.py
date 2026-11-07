"""查询理解：抽取实体/属性，补一路英文关键词，供多路召回使用。

成本控制：
- 结果按问题哈希缓存（LRU 256），重复问题不再调用模型；
- 单次 max_tokens=120、temperature=0，约 ¥0.0005；
- 任何失败都退回「原问题 + 规则改写」，不阻塞检索。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from app.integrations import llm

logger = logging.getLogger(__name__)

SYSTEM = (
    "把用户问题拆成检索用的关键词。输出 JSON："
    '{"entity": "核心实体", "attribute": "问的属性", "zh": ["中文关键词"], "en": ["英文关键词"]}。'
    "zh 与 en 各 2-4 个，只给词，不要句子，不要解释。"
)

# 常见术语的中英对照：模型不可用时也能补一路英文
GLOSSARY = {
    "切片": "chunk chunking",
    "检索": "retrieval",
    "重排": "rerank reranking",
    "向量": "embedding vector",
    "嵌入": "embedding",
    "知识图谱": "knowledge graph",
    "实体": "entity",
    "关系": "relation",
    "召回": "recall retrieval",
    "智能体": "agent",
    "多智能体": "multi-agent",
    "提示词": "prompt",
    "微调": "fine-tuning",
    "强化学习": "reinforcement learning",
    "幻觉": "hallucination",
    "引用": "citation",
    "摘要": "summary summarization",
    "索引": "index indexing",
    "上下文": "context",
}


@lru_cache(maxsize=256)
def understand(question: str) -> dict:
    """返回 {entity, attribute, zh, en}；失败时退回规则结果。"""
    fallback = _rule_based(question)
    try:
        raw = llm.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}],
            temperature=0,
            max_tokens=120,
            json_mode=True,
        )
        data = json.loads(raw)
    except Exception as exc:  # pragma: no cover - 依赖外部模型
        logger.debug("查询理解失败，使用规则结果：%s", exc)
        return fallback

    zh = [str(item).strip() for item in data.get("zh", []) if str(item).strip()][:4]
    en = [str(item).strip() for item in data.get("en", []) if str(item).strip()][:4]
    return {
        "entity": str(data.get("entity", "")).strip() or fallback["entity"],
        "attribute": str(data.get("attribute", "")).strip(),
        "zh": zh or fallback["zh"],
        "en": en,
    }


def _rule_based(question: str) -> dict:
    from app.services.retrieval import extract_entity

    entity = extract_entity(question)
    en: list[str] = []
    for zh_term, translation in GLOSSARY.items():
        if zh_term in question:
            en.extend(translation.split())
    return {"entity": entity, "attribute": "", "zh": [entity] if entity else [], "en": en[:4]}


COMPARE = re.compile(
    r"([\w\u4e00-\u9fff\-\.]{2,24})\s*(?:和|与|跟|vs\.?|对比|比较)\s*"
    r"([\w\u4e00-\u9fff\-\.]{2,24}?)\s*(?:有什么|有何|有哪些|的)?\s*(?:区别|差异|不同|对比|优劣)"
)


def _compare_queries(question: str) -> list[str]:
    """「A 和 B 的区别」拆成两路：只检索 A、只检索 B。

    对比题最容易翻车：一个实体在候选里、另一个被淹没，模型只能拒答。
    """
    match = COMPARE.search(question)
    if not match:
        # 查询改写后常变成「KTO DPO 有什么区别」，没有「和」字；
        # 这类两个拉丁词并列 + 对比意图的，也按对比题处理
        pair = re.search(
            r"([A-Za-z][A-Za-z0-9_.-]{1,20})\s+([A-Za-z][A-Za-z0-9_.-]{1,20})", question
        )
        if pair and any(word in question for word in ("区别", "对比", "不同", "差异", "vs")):
            return [pair.group(1), pair.group(2)]
        return []
    left, right = match.group(1).strip(), match.group(2).strip()
    return [item for item in (left, right) if item]


def extra_queries(question: str, limit: int = 3) -> list[str]:
    """把理解结果拼成额外的检索语句：对比题拆两路 + 中文关键词 + 英文关键词。"""
    compare = _compare_queries(question)
    info = understand(question)
    extras: list[str] = list(compare)

    if info["zh"]:
        joined = " ".join(info["zh"])
        if joined and joined not in question:
            extras.append(joined)
    if info["en"]:
        extras.append(" ".join(info["en"]))
    elif info["entity"]:
        # 纯英文实体：补一条带实体的短语，避免整句语义被稀释
        extras.append(info["entity"])

    seen: set[str] = set()
    unique: list[str] = []
    for item in extras:
        key = re.sub(r"\s+", " ", item).strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique[:limit]
