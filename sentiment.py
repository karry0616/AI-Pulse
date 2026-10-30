"""规则情感词表（唯一来源）：抓取初筛与 Analyst 降级共用同一口径。"""

from __future__ import annotations

import re

POSITIVE_WORDS = [
    "launch", "funding", "raises", "breakthrough", "partnership", "record",
    "surge", "release", "open-source", "wins", "growth", "boost",
    "发布", "融资", "突破", "增长", "合作", "开源", "上线", "刷新", "领跑",
]

NEGATIVE_WORDS = [
    "layoff", "lawsuit", "leak", "breach", "outage", "fine", "ban", "risk",
    "vulnerability", "scandal", "shutdown", "downturn", "controversy",
    "裁员", "泄露", "诉讼", "漏洞", "风险", "处罚", "封禁", "宕机", "争议", "下滑",
]


def _count_terms(text: str, terms: list[str]) -> int:
    """英文词按边界匹配（避免 risk-free 命中 risk，但允许复数 s），中文按子串计数。"""
    total = 0
    for term in terms:
        if term.isascii():
            total += len(re.findall(rf"(?<![a-z-]){re.escape(term)}s?(?![a-z-])", text))
        else:
            total += text.count(term)
    return total


def guess_sentiment(text: str) -> str:
    """轻量规则情感判定，真正的情感聚合后续交给 LLM。"""
    lowered = str(text or "").lower()
    pos = _count_terms(lowered, POSITIVE_WORDS)
    neg = _count_terms(lowered, NEGATIVE_WORDS)

    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"
