r"""每日 08:00 自动执行情报任务（独立进程，不依赖 Streamlit 会话）。

启动：
    .\.venv\Scripts\python.exe scheduler_service.py
手动跑一次：
    .\.venv\Scripts\python.exe scheduler_service.py --now
指定关键词：
    .\.venv\Scripts\python.exe scheduler_service.py --now --keywords "大模型,RAG"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import pipeline
from sources import DATA_SOURCES, KEYWORDS

DEFAULT_SOURCES = DATA_SOURCES
TIMEZONE = "Asia/Shanghai"


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def run_once(keyword: str, sources: list[str]) -> int:
    log(f"[SCHED] 开始每日任务：keyword={keyword} · sources={len(sources)}")
    try:
        result = pipeline.run(keyword, sources, trigger="schedule", log=log)
    except pipeline.RunLocked as exc:
        log(f"[SCHED] 已有任务在运行，本次跳过：{exc}")
        return 0
    except Exception as exc:
        log(f"[SCHED] 任务失败：{type(exc).__name__}: {exc}")
        return 1

    log(
        f"[SCHED] 任务完成 run_id={result.run_id} · 新增 {result.inserted} 条 · "
        f"模式 {result.analysis['_mode']} · 待审批预警 {result.alert_id or '无'}"
    )
    return 0


def run_all(keywords: list[str], sources: list[str]) -> int:
    """按关键词顺序串行执行；单个关键词失败不影响后续关键词。"""
    failed = 0
    for index, keyword in enumerate(keywords, start=1):
        log(f"[SCHED] ==== 关键词 {index}/{len(keywords)}：{keyword} ====")
        failed += run_once(keyword, sources)
    log(f"[SCHED] 本轮结束：{len(keywords)} 个关键词 · 失败 {failed} 个")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse 每日调度器")
    parser.add_argument("--now", action="store_true", help="立即执行一次后退出")
    parser.add_argument(
        "--keywords",
        default=",".join(KEYWORDS),
        help="逗号分隔的监控关键词，默认全部",
    )
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    keywords = [item.strip() for item in args.keywords.split(",") if item.strip()]
    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    if not keywords:
        log("[SCHED] 未指定任何关键词，退出")
        return 1

    if args.now:
        return run_all(keywords, sources)

    trigger = CronTrigger(hour=8, minute=0, timezone=TIMEZONE)
    scheduler = BlockingScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_all,
        trigger,
        args=[keywords, sources],
        id="ai_pulse_daily",
        name="AI Pulse 每日情报采集",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    next_run = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
    log(f"[SCHED] 调度器已启动，每天 08:00（{TIMEZONE}）执行 {len(keywords)} 个关键词")
    log(f"[SCHED] 关键词：{', '.join(keywords)}")
    log(f"[SCHED] 下次运行：{next_run}")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log("[SCHED] 调度器已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
