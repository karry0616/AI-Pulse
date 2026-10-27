"""历史记忆：召回近 7 天相似资讯 + 计算周同比负面情绪变化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import database

from . import store


@dataclass
class MemoryContext:
    similar: list[dict] = field(default_factory=list)
    comparison: str = "历史基线不足，暂无法计算周同比"
    negative_delta: float | None = None
    text: str = ""
    mode: str = "none"


def _week_over_week(keyword: str) -> tuple[str, float | None]:
    today = date.today()
    baseline_day = (today - timedelta(days=7)).isoformat()

    today_ratio = database.negative_ratio_on(today.isoformat(), keyword)
    baseline_ratio = database.negative_ratio_on(baseline_day, keyword)

    if today_ratio is None or baseline_ratio is None:
        return "历史基线不足，暂无法计算周同比", None

    delta = round(today_ratio - baseline_ratio, 1)
    if abs(delta) < 0.5:
        return "相比上周同期，该话题的负面情绪基本持平", delta
    verb = "上升" if delta > 0 else "下降"
    return f"相比上周同期，该话题的负面情绪{verb} {abs(delta):.1f}%", delta


def build_context(keyword: str, titles: list[str], log=print) -> MemoryContext:
    similar = store.query_similar(titles, k=6, days=7, log=log)
    comparison, delta = _week_over_week(keyword)

    lines = ["【历史上下文：近 7 天相似资讯】"]
    if similar:
        for item in similar:
            lines.append(
                f"- [{item['date']}] {item['title']}"
                f"（来源 {item['source'] or '未知'}，情感 {item['sentiment']}）"
            )
    else:
        lines.append("- 暂无相似历史资讯，本次按当日资讯独立判断")

    lines += ["", f"【历史对比】{comparison}", "请在结论中使用上述同比结论，不要自行编造百分比。"]
    text = "\n".join(lines)

    mode = similar[0]["mode"] if similar else "none"
    log(f"[RAG] {comparison}")
    return MemoryContext(
        similar=similar,
        comparison=comparison,
        negative_delta=delta,
        text=text,
        mode=mode,
    )
