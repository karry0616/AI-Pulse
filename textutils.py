"""文本归一化工具：标题 / URL 去重与相似度计算共用一套规则。

此前 database / events / critic / rag 各自实现了一份正则，口径容易漂移；
统一收敛到这里后，去重、事件归并、标题核验与向量降级检索的结果才能对齐。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见跟踪参数不参与 URL 判重
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "from",
    "ref",
    "fbclid",
    "gclid",
}
_NORM_RE = re.compile(r"[^\w]+")


def normalize(text) -> str:
    """去掉空白与标点、统一大小写，用于标题对齐与相似度。"""
    return _NORM_RE.sub("", str(text or "")).lower()


def bigrams(text) -> set[str]:
    """字符二元组集合，用于离线相似度计算。"""
    cleaned = normalize(text)
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def canonical_title(text) -> str:
    """标题指纹：跨来源同一标题即使标点/大小写不同也视为同一条。"""
    return normalize(text)


def canonical_url(url) -> str:
    """URL 指纹：去掉协议/主机大小写差异、末尾斜杠与跟踪参数。"""
    raw = str(url or "").strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        return normalize(raw)

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )
