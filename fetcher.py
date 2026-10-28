r"""RSS 抓取 → 清洗 → 简单情感打标 → 入库。

设计原则：只抓公开 RSS，不做深度爬虫；单源失败只跳过，不阻塞整体流程。

命令行自测：
    .\.venv\Scripts\python.exe fetcher.py --keyword "AI Agent"
"""

from __future__ import annotations

import argparse
import calendar
import html
import re
import sys
from datetime import date, datetime
from urllib.parse import quote

import feedparser
import requests

import database
from sources import KEYWORD_ALIASES, SEARCH_TERMS, SOURCES

USER_AGENT = "Mozilla/5.0 (compatible; AI-Pulse/0.1; +https://localhost)"
TIMEOUT = 10
SUMMARY_LIMIT = 160

POSITIVE_WORDS = [
    "launch", "funding", "raises", "breakthrough", "partnership", "record",
    "surge", "release", "open-source", "wins", "growth", "boost",
    "发布", "融资", "突破", "增长", "合作", "开源", "上线", "刷新", "领跑",
]

NEGATIVE_WORDS = [
    "layoff", "lawsuit", "leak", "breach", "outage", "fine", "ban", "risk",
    "vulnerability", "scandal", "shutdown", "downturn",
    "裁员", "泄露", "诉讼", "漏洞", "风险", "处罚", "封禁", "宕机", "争议", "下滑",
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int | None = None) -> str:
    """去 HTML 标签、反转义实体、压缩空白。"""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit] if limit else text


def entry_date(entry) -> str:
    """优先用发布时间（转本地日期），缺失则回落到今天。"""
    for key in ("published_parsed", "updated_parsed"):
        struct_time = entry.get(key)
        if struct_time:
            try:
                return datetime.fromtimestamp(calendar.timegm(struct_time)).date().isoformat()
            except (OverflowError, ValueError, OSError):
                continue
    return date.today().isoformat()


def match_keyword(text: str, keyword: str) -> bool:
    aliases = KEYWORD_ALIASES.get(keyword, [keyword])
    lowered = text.lower()
    return any(alias.lower() in lowered for alias in aliases)


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
    lowered = text.lower()
    pos = _count_terms(lowered, POSITIVE_WORDS)
    neg = _count_terms(lowered, NEGATIVE_WORDS)

    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def fetch_source(
    name: str,
    url: str,
    keyword: str,
    per_source: int,
    log,
) -> list[dict] | None:
    """抓取单个 RSS 源并过滤出与关键词相关的条目；抓取失败返回 None。"""
    if "{q}" in url:
        url = url.format(q=quote(SEARCH_TERMS.get(keyword, keyword)))

    resp = None
    for attempt in range(2):  # 公开 RSS 偶发超时，重试一次
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt == 1:
                log(f"[WARN] {name} 抓取失败：{type(exc).__name__}")
                return None

    if resp is None:
        return None

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        log(f"[WARN] {name} RSS 解析失败（无有效条目）")
        return None

    items: list[dict] = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""), 120)
        summary = clean_text(
            entry.get("summary") or entry.get("description") or "", SUMMARY_LIMIT
        )
        if not title:
            continue

        combined = f"{title} {summary}"
        if not match_keyword(combined, keyword):
            continue

        items.append(
            {
                "date": entry_date(entry),
                "keyword": keyword,
                "title": title,
                "summary": summary,
                "sentiment": guess_sentiment(combined),
                "source": name,
                "url": entry.get("link", ""),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

        if len(items) >= per_source:
            break

    log(f"[OK] {name} 命中 {len(items)} 条「{keyword}」资讯")
    return items


def fetch_all(
    sources: list[str],
    keyword: str = "AI Agent",
    per_source: int = 8,
    log=print,
) -> list[dict]:
    """遍历数据源，跨源按标题去重后返回。"""
    all_items: list[dict] = []
    seen: set[str] = set()

    for logical in sources:
        specs = SOURCES.get(logical)
        if not specs:
            log(f"[WARN] {logical} 暂无公开 RSS 源，已跳过")
            continue

        items: list[dict] | None = None
        for label, url in specs:
            items = fetch_source(label, url, keyword, per_source, log)
            if items is not None:
                if len(specs) > 1 and label != logical:
                    log(f"[WARN] {logical} 源不可用，已回退至 {label}")
                break

        if items is None:
            log(f"[WARN] {logical} 全部候选源抓取失败，已跳过")
            continue

        for item in items:
            key = item["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            all_items.append(item)

    return all_items


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse RSS 抓取自测")
    parser.add_argument("--keyword", default="AI Agent", help="监控关键词")
    parser.add_argument("--per-source", type=int, default=8, help="单源最多抓取条数")
    args = parser.parse_args()

    def log(message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}")

    database.init_db()
    log(f"开始抓取 {len(SOURCES)} 个 RSS 源，关键词「{args.keyword}」")

    items = fetch_all(list(SOURCES), args.keyword, args.per_source, log)
    inserted = database.save_items(items)

    log(f"抓取完成：共 {len(items)} 条，实际新增入库 {inserted} 条（重复已忽略）")

    for item in items[:5]:
        print(f"  - [{item['source']}] {item['date']} {item['sentiment']:>8}  {item['title'][:60]}")

    stats = database.latest_snapshot(args.keyword)
    log(f"今日快照：{stats}")

    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
