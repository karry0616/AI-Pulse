"""事件线与实体追踪：把同一条新闻的多来源报道归并成一条持续跟踪的事件线。

归并判定完全由确定性代码完成——标题字符二元组重合度为主、Collector 给出的
实体命中为辅，不额外调用 LLM，因此结果可复现、可解释、零 token 成本。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date

import database
from textutils import bigrams, normalize

WINDOW_DAYS = 7          # 事件线的活跃窗口：超过 7 天未更新的线不再参与归并
SIMILARITY = 0.5         # 标题相似度阈值（字符二元组 Jaccard）
ENTITY_BOOST = 0.2       # 实体命中时相似度的加成
MIN_SHARED_GRAMS = 3     # 至少共享的二元组数量，防止短标题被误合并


def _norm(text) -> str:
    """去掉空白与标点、统一大小写。"""
    return normalize(text)


def _bigrams(text: str) -> set[str]:
    return bigrams(text)


def _title_score(left: str, right: str) -> float:
    """两条标题的字符二元组 Jaccard 相似度；共享过少直接判 0。"""
    grams_left, grams_right = _bigrams(left), _bigrams(right)
    if not grams_left or not grams_right:
        return 0.0
    shared = grams_left & grams_right
    if len(shared) < MIN_SHARED_GRAMS:
        return 0.0
    return len(shared) / len(grams_left | grams_right)


def _event_index(events: list[dict]) -> dict[str, dict]:
    """事件标题归一化 → {实体列表, 情感}，供资讯标题反查。"""
    index: dict[str, dict] = {}
    for event in events or []:
        key = _norm(event.get("title"))
        if not key:
            continue
        entities = [str(entity).strip() for entity in (event.get("entities") or [])]
        index[key] = {
            "entities": [entity for entity in entities if entity][:5],
            "sentiment": str(event.get("sentiment") or "").lower(),
        }
    return index


def match_event(title: str, index: dict[str, dict]) -> dict:
    """按「精确匹配 → 包含关系」从 Collector 结果里找回实体与情感。"""
    key = _norm(title)
    if key:
        if key in index:
            return index[key]
        for candidate, meta in index.items():
            if candidate and (candidate in key or key in candidate):
                return meta
    return {"entities": [], "sentiment": ""}


def thread_key(keyword: str, entity: str, title: str) -> str:
    """事件线唯一键：实体 + 标题指纹，避免同名实体下不同事件互相覆盖。"""
    raw = f"{keyword}|{_norm(entity)}|{_norm(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _best_thread(title: str, entities: list[str], threads: list[dict]) -> dict | None:
    """在候选事件线中找最匹配的一条；实体命中可降低标题相似度门槛。"""
    entity_keys = {_norm(entity) for entity in entities if _norm(entity)}
    best: dict | None = None
    best_score = 0.0

    for thread in threads:
        score = _title_score(title, str(thread.get("title") or ""))
        owner = _norm(thread.get("entity"))
        if entity_keys and owner and owner in entity_keys:
            score += ENTITY_BOOST
        if score > best_score:
            best, best_score = thread, score

    return best if best is not None and best_score >= SIMILARITY else None


def link_run(
    items: list[dict],
    analysis: dict,
    keyword: str,
    log=print,
) -> dict:
    """把本次分析的资讯归并进事件线，返回汇总供日志 / 页面 / 日报使用。"""
    rows = [item for item in items if str(item.get("title") or "").strip()]
    if not rows:
        return {"linked": 0, "new": 0, "threads": 0, "window_days": WINDOW_DAYS, "top": []}

    index = _event_index(analysis.get("_events_detail") or [])
    threads = database.recent_threads(keyword, days=WINDOW_DAYS)

    linked = created = 0
    touched: set[int] = set()

    for item in rows:
        title = str(item.get("title") or "").strip()
        day = str(item.get("date") or date.today().isoformat())
        meta = match_event(title, index)
        sentiment = meta.get("sentiment") or str(item.get("sentiment") or "neutral")
        entities = meta.get("entities") or []
        entity = entities[0] if entities else ""

        matched = _best_thread(title, entities, threads)
        if matched is None:
            key = thread_key(keyword, entity, title)
            thread_id = database.create_thread(
                thread_key=key,
                keyword=keyword,
                title=title,
                day=day,
                entity=entity,
                sentiment=sentiment,
            )
            threads = [
                {
                    "id": thread_id,
                    "entity": entity,
                    "title": title,
                    "first_date": day,
                    "last_date": day,
                    "item_count": 0,
                    "latest_sentiment": sentiment,
                }
            ] + threads
            created += 1
        else:
            thread_id = int(matched["id"])

        if database.link_thread_item(thread_id, item):
            database.touch_thread(thread_id, day, sentiment, entity)
            linked += 1
            touched.add(thread_id)

    if linked:
        log(
            f"[EVENT] 归并 {linked} 条资讯到 {len(touched)} 条事件线"
            f"（新建 {created} 条，窗口 {WINDOW_DAYS} 天）"
        )

    return {
        "linked": linked,
        "new": created,
        "threads": len(touched),
        "window_days": WINDOW_DAYS,
        "top": database.top_threads(keyword, days=WINDOW_DAYS, limit=5),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="事件线追踪自测")
    parser.add_argument("--keyword", default="AI Agent")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    database.init_db()
    threads = database.top_threads(args.keyword, days=args.days, limit=10)
    if not threads:
        print(f"[EVENT] 「{args.keyword}」近 {args.days} 天没有事件线")
        return 0

    print(f"[EVENT] 「{args.keyword}」近 {args.days} 天共 {len(threads)} 条事件线：")
    for thread in threads:
        entity = thread.get("entity") or "-"
        print(
            f"  #{thread['id']} [{entity}] {thread['title'][:50]} · "
            f"{thread['item_count']} 篇 · {thread['first_date']} → {thread['last_date']} · "
            f"{thread['latest_sentiment']}"
        )
        for row in database.thread_timeline(thread["id"], limit=5):
            print(f"      {row['date']} [{row['source']}] {row['title'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
