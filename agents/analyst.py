"""Analyst Agent：候选事件 + 历史上下文 → 结构化 JSON。"""

from __future__ import annotations

from llm import client

from . import prompts
from .state import AgentState

NEGATIVE_WORDS = [
    "layoff", "lawsuit", "leak", "breach", "outage", "fine", "ban", "risk",
    "vulnerability", "scandal", "shutdown", "downturn", "controversy",
    "裁员", "泄露", "诉讼", "漏洞", "风险", "处罚", "封禁", "宕机", "争议", "下滑",
]

POSITIVE_WORDS = [
    "launch", "funding", "raises", "breakthrough", "partnership", "record",
    "surge", "release", "open-source", "wins", "growth",
    "发布", "融资", "突破", "增长", "合作", "开源", "上线",
]

FALLBACK_MATURITY = """| 方向 | 成熟度 | 阶段判断 |
| :--- | :--- | :--- |
| 大模型 | ●●●●○ | 生产可用，能力差异化收敛 |
| AI Agent | ●●●○○ | 早期规模化，可靠性待验证 |
| RAG | ●●●●○ | 工程化成熟，进入默认配置 |
| 具身智能 | ●●○○○ | 概念验证，数据链路是瓶颈 |"""


def fallback(state: AgentState, reason: str, log=print) -> dict:
    """无 Key / API 失败时的规则结果，保证前端永远有数据。"""
    titles = state.titles
    total = len(titles)
    negatives = [t for t in titles if any(word in t.lower() for word in NEGATIVE_WORDS)]
    positives = [t for t in titles if any(word in t.lower() for word in POSITIVE_WORDS)]
    negative_hits, positive_hits = len(negatives), len(positives)

    heat = max(800, min(2000, 820 + 70 * min(total, 15)))
    ratio = round(positive_hits / total * 100) if total else 0

    top_news = "\n".join(
        f"- **{title[:38]}**：来自今日 RSS 抓取，待人工复核。" for title in titles[:4]
    ) or "- **暂无动态**：今日未抓到相关资讯。"

    warning = (
        f"规则引擎监测到 {negative_hits} 条潜在负面资讯，涉及裁员、诉讼或数据风险信号，建议人工复核。"
        if negative_hits
        else "暂未监测到高风险事件，负面信号密度低于阈值，维持常规观察。"
    )

    log(f"[WARN] Analyst 进入本地规则模式：{reason}")
    state.mode = "fallback"
    state.degraded = True
    return {
        "heat_score": heat,
        "positive_ratio": f"{ratio}%",
        "negative_alerts": min(negative_hits, 5),
        "top_news": top_news,
        "maturity": FALLBACK_MATURITY,
        "warning_text": warning,
        "comparison": state.memory.comparison,
    }


def run(state: AgentState, log=print) -> dict:
    """调用 DeepSeek 输出结构化 JSON；失败自动降级，解析失败交给 Critic 打回。"""
    if not state.titles:
        return fallback(state, "今日没有抓到资讯", log)

    if not client.available():
        return fallback(state, "未设置 DEEPSEEK_API_KEY", log)

    messages = prompts.analyst_messages(
        state.events,
        state.titles,
        state.keyword,
        state.memory.text,
        state.critique,
    )

    try:
        result = client.chat(
            messages,
            node="Analyst",
            run_id=state.run_id,
            log=log,
            max_tokens=1400,
        )
    except (client.LLMUnavailableError, client.LLMCallError) as exc:
        return fallback(state, f"{type(exc).__name__}: {exc}", log)

    payload = client.extract_json(result.content)
    if payload is None:
        log("[WARN] Analyst 输出不是合法 JSON，交由 Critic 审核")
        return {"_raw": result.content[:600]}

    state.mode = "deepseek"
    return payload
