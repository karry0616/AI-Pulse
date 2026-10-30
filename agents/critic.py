"""Critic Agent：JSON 契约校验 + 数值一致性检查 + 结果归一。"""

from __future__ import annotations

import re

from textutils import normalize

from .analyst import FALLBACK_MATURITY
from .state import AgentState

REQUIRED_KEYS = (
    "heat_score",
    "positive_ratio",
    "negative_alerts",
    "top_news",
    "maturity",
    "warning_text",
    "comparison",
)

RATIO_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")
MATURITY_HEADER = "| 方向 | 成熟度 | 阶段判断 |"
BULLET_RE = re.compile(r"^\s*-\s*\*\*(.+?)\*\*")


def _normalize(text: str) -> str:
    """去掉空白与标点，统一大小写，便于标题核对。"""
    return normalize(text)


def _matches_input_title(head: str, titles: list[str]) -> bool:
    """短标题需能在输入标题里找到（包含关系或字符重合度 ≥ 0.6）。"""
    normalized = _normalize(head)
    if len(normalized) < 2:
        return False

    for title in titles:
        candidate = _normalize(title)
        if not candidate:
            continue
        if normalized in candidate or candidate in normalized:
            return True
        overlap = len(set(normalized) & set(candidate)) / len(set(normalized))
        if overlap >= 0.6:
            return True
    return False


def _as_int(value) -> int | None:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def check_contract(draft: dict | None, state: AgentState) -> list[str]:
    """返回打回理由；空列表代表通过。"""
    if not isinstance(draft, dict):
        return ["输出不是 JSON 对象"]
    if "_raw" in draft:
        return ["输出无法解析为合法 JSON"]

    issues: list[str] = []
    missing = [key for key in REQUIRED_KEYS if key not in draft]
    if missing:
        issues.append(f"缺少字段：{', '.join(missing)}")

    heat = _as_int(draft.get("heat_score"))
    if heat is None or not 800 <= heat <= 2000:
        issues.append(f"heat_score 必须是 800-2000 的整数，实际为 {draft.get('heat_score')!r}")

    ratio_raw = str(draft.get("positive_ratio") or "").strip()
    match = RATIO_RE.match(ratio_raw)
    if not match:
        issues.append(f'positive_ratio 必须是百分数字符串（如 "65%"），实际为 {ratio_raw!r}')
    elif not 0 <= float(match.group(1)) <= 100:
        issues.append(f"positive_ratio 超出 0-100：{ratio_raw}")

    alerts = _as_int(draft.get("negative_alerts"))
    if alerts is None or not 0 <= alerts <= 5:
        issues.append(f"negative_alerts 必须是 0-5 的整数，实际为 {draft.get('negative_alerts')!r}")
    elif state.items and alerts > len(state.items):
        issues.append(f"negative_alerts={alerts} 超过输入资讯条数 {len(state.items)}")

    top_news = str(draft.get("top_news") or "")
    bullets = [line for line in top_news.splitlines() if line.strip().startswith("- ")]
    if len(bullets) < 3:
        issues.append(f"top_news 至少需要 3 条 `- ` 列表，实际 {len(bullets)} 条")
    else:
        verified = 0
        for line in bullets:
            match = BULLET_RE.match(line)
            if match and _matches_input_title(match.group(1), state.titles):
                verified += 1
        if verified < 3:
            issues.append(
                f"top_news 至少 3 条短标题需来自输入标题原文，实际可核验 {verified} 条"
            )

    maturity = str(draft.get("maturity") or "")
    rows = [line for line in maturity.splitlines() if line.strip().startswith("|")]
    if MATURITY_HEADER not in maturity or len(rows) < 4:
        issues.append("maturity 必须是表头为 | 方向 | 成熟度 | 阶段判断 | 的 3-5 行表格")

    warning = str(draft.get("warning_text") or "").strip()
    if not 30 <= len(warning) <= 200:
        issues.append(f"warning_text 长度需在 30-200 字之间，实际 {len(warning)} 字")

    comparison = str(draft.get("comparison") or "").strip()
    if not comparison:
        issues.append("comparison 不能为空，必须复用【历史对比】结论")

    return issues


def finalize(state: AgentState) -> dict:
    """把通过审核的草稿归一成 UI 可直接消费的结构。"""
    draft = state.draft or {}

    heat = _as_int(draft.get("heat_score")) or 1000
    heat = max(800, min(2000, heat))

    match = re.search(r"\d+(?:\.\d+)?", str(draft.get("positive_ratio") or "60"))
    ratio = max(0.0, min(100.0, float(match.group()) if match else 60.0))

    alerts = _as_int(draft.get("negative_alerts")) or 0
    alerts = max(0, min(5, alerts))

    def text(key: str, default: str) -> str:
        value = str(draft.get(key) or "").strip()
        return value or default

    last_issues = next(
        (issues for issues in reversed(state.critique_history) if issues),
        state.critique,
    )

    return {
        "heat_score": heat,
        "positive_ratio": f"{ratio:.0f}%",
        "negative_alerts": alerts,
        "top_news": text("top_news", "- **暂无动态**：今日资讯不足以形成判断。"),
        "maturity": text("maturity", FALLBACK_MATURITY),
        "warning_text": text("warning_text", "暂无高风险事件"),
        "comparison": text("comparison", state.memory.comparison),
        "_mode": state.mode,
        "_attempts": state.attempts,
        "_degraded": state.degraded,
        "_passed": state.passed,
        "_issues": list(last_issues),
        "_events": len(state.events),
        "_events_detail": [
            {
                "title": event.get("title", ""),
                "sentiment": event.get("sentiment", "neutral"),
                "entities": list(event.get("entities") or [])[:5],
            }
            for event in state.events
        ],
        "_run_id": state.run_id,
    }
