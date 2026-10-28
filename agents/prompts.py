"""Agent 全部 Prompt 集中管理。"""

from __future__ import annotations

from datetime import date

COLLECTOR_SYSTEM = """你是 AI Pulse 的 Collector Agent，负责从新闻列表中筛出与监控关键词相关的候选事件。
只输出一个 json 对象，禁止解释、禁止 Markdown 代码块包裹。

输出格式：
{"events": [{"title": "原始标题", "entities": ["公司或技术名"], "sentiment": "positive|neutral|negative", "reason": "不超过 30 字的判断依据"}]}

约束：
- events 最多 30 条，按重要性从高到低排序；与关键词无关的条目直接丢弃。
- sentiment 只能取 positive / neutral / negative 三值之一。
- title 必须原样保留输入标题，不得改写或编造。
- 没有相关条目时返回 {"events": []}。"""

ANALYST_SYSTEM = """你是 AI Pulse 的 Analyst Agent，负责把候选事件压缩成结构化行业判断。
你只输出一个 json 对象，禁止输出解释、禁止 Markdown 代码块包裹、禁止出现多余字段。

字段与取值约束（必须全部返回）：
- "heat_score": 整数，800-2000。根据事件条数、命中源数量、话题集中度估算。
- "positive_ratio": 字符串，形如 "65%"，0%-100%，表示正面或中性偏积极内容占比。
- "negative_alerts": 整数，0-5，代表需要人工复核的高风险事件条数，不得超过输入事件总数。
- "top_news": Markdown 无序列表字符串，3-5 条，每条格式为 "- **短标题**：一句话解读"，条目之间用 \\n 分隔。短标题必须来自输入事件/标题原文（可截断，不得改写、合并或编造）。
- "maturity": Markdown 表格字符串，表头固定为 | 方向 | 成熟度 | 阶段判断 |，3-5 行，成熟度用 ●○○ 表示。
- "warning_text": 纯文本，60-120 字，概括最值得警惕的负面信号；若确实没有风险，写"暂无高风险事件"。
- "comparison": 字符串，直接使用用户消息中【历史对比】给出的结论，不得自行编造百分比。

内容全部使用中文。不要编造输入中不存在的事实。"""

RETRY_TEMPLATE = """Critic 审核未通过，问题如下：
{issues}

请修正后重新只输出完整合法的 json 对象，字段必须齐全且符合契约。"""


def collector_messages(items: list[dict], keyword: str) -> list[dict]:
    lines = []
    for idx, item in enumerate(items[:30], start=1):
        title = str(item.get("title") or "")[:120]
        summary = str(item.get("summary") or "")[:100]
        source = item.get("source") or "未知"
        lines.append(f"{idx}. [{source}] {title} | {summary}")

    user_prompt = (
        f"监控关键词：{keyword}\n"
        f"日期：{date.today().isoformat()}\n"
        f"候选资讯共 {min(len(items), 30)} 条：\n"
        + "\n".join(lines)
        + "\n\n请输出 events json。"
    )
    return [
        {"role": "system", "content": COLLECTOR_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]


def analyst_messages(
    events: list[dict],
    titles: list[str],
    keyword: str,
    memory_text: str,
    critique: list[str] | None = None,
) -> list[dict]:
    if events:
        body = "\n".join(
            f"- [{event.get('sentiment')}] {event.get('title')}"
            f"（{event.get('reason') or '无补充'}）"
            for event in events[:30]
        )
    else:
        body = "\n".join(f"- {title}" for title in titles[:30])

    user_prompt = (
        f"监控关键词：{keyword}\n"
        f"日期：{date.today().isoformat()}\n\n"
        f"{memory_text}\n\n"
        f"今日候选事件：\n{body}\n\n"
        "请按契约输出 json。"
    )

    messages = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    if critique:
        messages.append(
            {"role": "user", "content": RETRY_TEMPLATE.format(issues="；".join(critique))}
        )
    return messages
