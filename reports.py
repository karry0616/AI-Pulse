"""把一次 run 的分析结果导出为 Markdown 日报（reports/ 目录，不入库）。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _slug(keyword: str) -> str:
    cleaned = "".join(ch for ch in keyword if ch.isalnum() or ch in "-_ ").strip()
    return cleaned or "unknown"


def render(
    analysis: dict,
    keyword: str,
    run_id: str,
    items: int = 0,
    inserted: int = 0,
    usage: dict | None = None,
    alert_id: int | None = None,
    level: str = "",
) -> str:
    usage = usage or {}
    alert_line = f"#{alert_id}（{level}）" if alert_id else "无"
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    return f"""# AI Pulse 日报 · {keyword}

- 日期：{date.today().isoformat()}
- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- run_id：`{run_id}`
- 抓取：{items} 条 · 新增入库 {inserted} 条
- 热度：{analysis.get("heat_score", "-")} / 2000
- 正面占比：{analysis.get("positive_ratio", "-")}
- 负面预警：{analysis.get("negative_alerts", 0)} 条 · 待审批任务 {alert_line}
- 分析模式：{analysis.get("_mode", "-")} · Critic 尝试 {analysis.get("_attempts", 0)} 次

## 核心动态

{analysis.get("top_news", "-")}

## 事件线追踪

{thread_lines(analysis.get("_event_threads") or {})}

## 成熟度

{analysis.get("maturity", "-")}

## 风险预警

{alert_line_display(alert_id, level, analysis.get("warning_text", ""))}

## 历史对比

{analysis.get("comparison", "-")}

## 成本

- LLM 调用：{usage.get("calls", 0)} 次
- tokens：{usage.get("total_tokens", 0)}（prompt {prompt_tokens} / completion {completion_tokens}）
- 调用累计耗时：{usage.get("latency_ms", 0) / 1000:.1f}s
- 端到端耗时：{(analysis.get("_duration_ms") or 0) / 1000:.1f}s
"""


def alert_line_display(alert_id: int | None, level: str, warning_text: str) -> str:
    if alert_id:
        return f"> {level} · 预警 #{alert_id} 等待人工审核\n\n{warning_text}"
    return warning_text or "暂无高风险事件"


def thread_lines(info: dict) -> str:
    """事件线追踪段落：同一事件的多来源报道归并成一条时间线。"""
    threads = info.get("top") or []
    if not threads:
        return "暂无事件线（首次采集或资讯互不相关）"

    lines = [
        f"- **{thread.get('title', '-')}** · 实体 {thread.get('entity') or '未识别'}"
        f" · {int(thread.get('item_count') or 0)} 篇报道"
        f" · {thread.get('first_date') or '-'} → {thread.get('last_date') or '-'}"
        for thread in threads[:5]
    ]
    return "\n".join(lines)


def save(
    analysis: dict,
    keyword: str,
    run_id: str,
    items: int = 0,
    inserted: int = 0,
    usage: dict | None = None,
    alert_id: int | None = None,
    level: str = "",
    directory: Path | None = None,
) -> Path:
    """写入 reports/YYYY-MM-DD-{keyword}.md，返回文件路径。"""
    target_dir = directory or REPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{date.today().isoformat()}-{_slug(keyword)}.md"
    path.write_text(
        render(analysis, keyword, run_id, items, inserted, usage, alert_id, level),
        encoding="utf-8",
    )
    return path
