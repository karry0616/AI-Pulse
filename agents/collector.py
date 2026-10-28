"""Collector Agent：把资讯清洗成候选事件。"""

from __future__ import annotations

from llm import client

from . import prompts
from .state import AgentState

VALID_SENTIMENTS = {"positive", "neutral", "negative"}


def _fallback_events(items: list[dict]) -> list[dict]:
    events: list[dict] = []
    for item in items[:30]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        sentiment = str(item.get("sentiment") or "neutral").lower()
        events.append(
            {
                "title": title,
                "entities": [],
                "sentiment": sentiment if sentiment in VALID_SENTIMENTS else "neutral",
                "reason": "RSS 规则情感判定",
            }
        )
    return events


def _sanitize(raw_events) -> list[dict]:
    if not isinstance(raw_events, list):
        return []

    events: list[dict] = []
    seen: set[str] = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title or title in seen:
            continue
        sentiment = str(raw.get("sentiment") or "neutral").lower()
        events.append(
            {
                "title": title,
                "entities": [str(entity) for entity in (raw.get("entities") or [])][:5],
                "sentiment": sentiment if sentiment in VALID_SENTIMENTS else "neutral",
                "reason": str(raw.get("reason") or "")[:60],
            }
        )
        seen.add(title)
        if len(events) >= 30:
            break
    return events


def run(state: AgentState, log=print) -> list[dict]:
    if not state.items:
        state.events = []
        log("[AGENT] Collector 没有可提取的资讯")
        return []

    if not client.available():
        state.events = _fallback_events(state.items)
        state.degraded = True
        log(f"[AGENT] Collector 规则提取 {len(state.events)} 个候选事件（未配置 API Key）")
        return state.events

    try:
        result = client.chat(
            prompts.collector_messages(state.items, state.keyword),
            node="Collector",
            run_id=state.run_id,
            log=log,
            max_tokens=1500,
        )
    except (client.LLMUnavailableError, client.LLMCallError) as exc:
        state.events = _fallback_events(state.items)
        state.degraded = True
        log(f"[WARN] Collector 调用失败（{type(exc).__name__}），回退规则提取")
        return state.events

    payload = client.extract_json(result.content) or {}
    events = _sanitize(payload.get("events"))

    if not events:
        state.events = _fallback_events(state.items)
        state.degraded = True
        log("[WARN] Collector 输出为空或格式异常，回退规则提取")
    else:
        state.events = events
        log(f"[AGENT] Collector 提取 {len(events)} 个候选事件（LLM）")

    return state.events
