"""运行指标：把 run_logs 里的执行结果汇总成可看、可对照的运维数字。

指标直接读 SQLite 的 run_logs，不额外维护计数器，避免出现「计数器与真实
记录对不上」的经典问题。每次 run 结束时由 pipeline 写入：

    sources_ok / sources_total   抓取成功率
    rejections                   Critic 打回次数
    degraded                     是否走了规则降级
    duration_ms / fetch_ms       全链路与抓取耗时

命令行自测：
    .\\.venv\\Scripts\\python.exe metrics.py --days 30
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

import database

DEFAULT_WINDOW_DAYS = 30


def percentile(values: list[int | float], p: float) -> float | None:
    """线性插值分位数（p 取 0-100），空列表返回 None。

    样本量很小时不假装精确：少于 3 个样本只返回最大值，
    避免 2 个数据点算出「P95」这种没有意义的数字。
    """
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) < 3:
        return ordered[-1]
    position = (len(ordered) - 1) * p / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _window(days: int) -> str:
    return (date.today() - timedelta(days=max(days - 1, 0))).isoformat()


def _usable(row: dict) -> bool:
    """该 run 能否进入耗时与条数统计。

    断网日（sources_ok=0）和「抓到 0 条、回退最近存档」的 run 都会以
    status="success" 落库，但它们的耗时反而更短、条数为 0，混进分位数会把
    指标刷得虚好看。因此只统计「既有成功抓取、又有真实新资讯」的 run。

    sources_total=0 的是 schema v5 之前的老行（当时还没有抓取统计），
    无法判断是否断网，只要 items>0 就照常计入，避免历史数据被误伤。
    """
    if row.get("status") != "success" or int(row.get("items") or 0) <= 0:
        return False
    total = int(row.get("sources_total") or 0)
    return total == 0 or int(row.get("sources_ok") or 0) > 0


def recent_run_rows(days: int = DEFAULT_WINDOW_DAYS, limit: int = 200) -> list[dict]:
    """窗口内最近的运行记录（按开始时间倒序）。"""
    since = _window(days)
    with database._connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, keyword, trigger, mode, status, items, inserted,
                   duration_ms, fetch_ms, sources_ok, sources_total,
                   rejections, degraded, started_at, finished_at, error
              FROM run_logs
             WHERE COALESCE(started_at, finished_at, '') >= ?
             ORDER BY COALESCE(started_at, '') DESC, rowid DESC
             LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def series(limit: int = 20) -> list[dict]:
    """最近 N 次有效 run 的耗时段，供页面画趋势。

    只取既有成功抓取、又有真实新资讯的 run，避免断网日或归档回退的
    「假成功」把趋势线压出一个虚假的低谷。
    """
    rows = recent_run_rows(days=90, limit=limit * 3)
    done = [row for row in rows if _usable(row)]
    done = list(reversed(done[:limit]))
    return [
        {
            "label": str(row.get("finished_at") or row.get("started_at") or "")[5:16],
            "duration_ms": int(row.get("duration_ms") or 0),
            "mode": row.get("mode") or "-",
            "degraded": int(row.get("degraded") or 0),
        }
        for row in done
    ]


def summary(days: int = DEFAULT_WINDOW_DAYS, keyword: str | None = None) -> dict:
    """窗口内的运行健康度：成功率、降级率、抓取成功率、耗时分位、打回次数。"""
    rows = recent_run_rows(days=days, limit=1000)
    if keyword:
        rows = [row for row in rows if row["keyword"] == keyword]

    if not rows:
        return {
            "window_days": days,
            "runs": 0,
            "usable_runs": 0,
            "excluded_runs": 0,
            "success_rate": None,
            "degraded_rate": None,
            "fetch_ok_ratio": None,
            "rejections_per_run": None,
            "p50_ms": None,
            "p95_ms": None,
            "avg_ms": None,
            "avg_fetch_ms": None,
            "avg_items": None,
        }

    done = [row for row in rows if row["status"] == "success"]
    usable = [row for row in rows if _usable(row)]
    durations = [int(row.get("duration_ms") or 0) for row in usable if row.get("duration_ms")]
    fetch_durations = [int(row.get("fetch_ms") or 0) for row in usable if row.get("fetch_ms")]
    # 抓取成功率按全部成功 run 统计：断网日就该把比例拉下来，这是健康信号，不能排除
    sources_total = sum(int(row.get("sources_total") or 0) for row in done)
    sources_ok = sum(int(row.get("sources_ok") or 0) for row in done)
    degraded = sum(int(row.get("degraded") or 0) for row in done)

    return {
        "window_days": days,
        "runs": len(rows),
        "usable_runs": len(usable),
        "excluded_runs": len(done) - len(usable),
        "success_rate": round(len(done) / len(rows) * 100, 1),
        "degraded_rate": round(degraded / len(done) * 100, 1) if done else None,
        "fetch_ok_ratio": round(sources_ok / sources_total * 100, 1) if sources_total else None,
        "rejections_per_run": (
            round(sum(int(row.get("rejections") or 0) for row in done) / len(done), 2)
            if done
            else None
        ),
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
        "avg_ms": round(sum(durations) / len(durations)) if durations else None,
        "avg_fetch_ms": (
            round(sum(fetch_durations) / len(fetch_durations)) if fetch_durations else None
        ),
        "avg_items": round(sum(int(row.get("items") or 0) for row in usable) / len(usable), 1)
        if usable
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse 运行指标")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--keyword", default=None)
    args = parser.parse_args()

    database.init_db()
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary(days=args.days, keyword=args.keyword),
        "series": series(limit=20),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
