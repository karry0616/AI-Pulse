"""Analyst Agent：候选事件 + 历史上下文 → 结构化 JSON。"""

from __future__ import annotations

from llm import client
from sentiment import guess_sentiment

from . import prompts
from .state import AgentState

FALLBACK_MATURITY = """| 方向 | 成熟度 | 阶段判断 |
| :--- | :--- | :--- |
| 大模型 | ●●●●○ | 生产可用，能力差异化收敛 |
| AI Agent | ●●●○○ | 早期规模化，可靠性待验证 |
| RAG | ●●●●○ | 工程化成熟，进入默认配置 |
| 具身智能 | ●●○○○ | 概念验证，数据链路是瓶颈 |"""

# 与 Critic 的契约一致：兜底文案必须落在 30-200 字区间，
# 否则规则模式的输出永远过不了契约，每次降级都会白跑一轮打回。
WARNING_MIN = 30
WARNING_MAX = 200


def _ensure_warning(text: str, fallback_tail: str) -> str:
    """把兜底文案夹到 30-200 字，避免规则模式输出违反 Critic 契约。"""
    text = (text or "").strip()
    if len(text) < WARNING_MIN:
        text = f"{text}{fallback_tail}".strip()
    return text[:WARNING_MAX]


def fallback(state: AgentState, reason: str, log=print) -> dict:
    """无 Key / API 失败时的规则结果，保证前端永远有数据。"""
    titles = state.titles
    total = len(titles)
    negatives = [t for t in titles if guess_sentiment(t) == "negative"]
    positives = [t for t in titles if guess_sentiment(t) == "positive"]
    negative_hits, positive_hits = len(negatives), len(positives)

    heat = max(800, min(2000, 820 + 70 * min(total, 15)))
    ratio = round(positive_hits / total * 100) if total else 0

    top_news = "\n".join(
        f"- **{title[:38]}**：来自今日 RSS 抓取，待人工复核。" for title in titles[:4]
    ) or "- **暂无动态**：今日未抓到相关资讯。"

    warning = _ensure_warning(
        (
            f"规则引擎监测到 {negative_hits} 条潜在负面资讯，涉及裁员、诉讼或数据风险信号。"
            if negative_hits
            else "暂未监测到高风险事件，负面信号密度低于阈值。"
        ),
        "建议人工复核后再决定是否推送；规则模式仅做信号提示，不作为结论依据。",
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
