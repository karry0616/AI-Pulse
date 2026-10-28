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
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import pipeline
from sources import DATA_SOURCES, KEYWORDS

DEFAULT_SOURCES = DATA_SOURCES
TIMEZONE = "Asia/Shanghai"

BASE_DIR = Path(__file__).resolve().parent
DISABLED_FLAG = BASE_DIR / ".scheduler.off"   # 存在即拒绝启动，防止任何入口重新拉起
PID_FILE = BASE_DIR / ".scheduler.pid"


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def scheduler_running() -> int | None:
    """返回正在运行的调度器 pid；没有或已退出返回 None，并清理残留 pid 文件。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None
    if pid > 0 and pipeline.pid_alive(pid):
        return pid
    PID_FILE.unlink(missing_ok=True)
    return None


def stop_scheduler() -> bool:
    """终止常驻调度器进程（只针对 pid 文件记录的进程，不碰其他 python）。"""
    pid = scheduler_running()
    if pid is None:
        log("[SCHED] 没有正在运行的调度进程")
        return False
    if pid == os.getpid():
        log("[SCHED] 当前进程即调度器，请用 Ctrl+C 结束")
        return False

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        os.kill(pid, 15)
    PID_FILE.unlink(missing_ok=True)
    log(f"[SCHED] 已终止调度进程 pid {pid}")
    return True


def disable_scheduler() -> None:
    """停止并禁止每日定时任务：写标记文件，任何后续启动都会被拒绝。"""
    DISABLED_FLAG.write_text(
        f"disabled_at={datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    stop_scheduler()
    log("[SCHED] 每日定时任务已关闭（.scheduler.off 已写入，启动时会被拒绝）")


def enable_scheduler() -> None:
    DISABLED_FLAG.unlink(missing_ok=True)
    log("[SCHED] 已解除定时任务禁用标记，需要手动启动 scheduler_service.py 才会运行")


def run_once(keyword: str, sources: list[str]) -> int:
    log(f"[SCHED] 开始每日任务：keyword={keyword} · sources={len(sources)}")
    try:
        result = pipeline.run(keyword, sources, trigger="schedule", log=log)
    except pipeline.RunLockedError as exc:
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
    parser.add_argument("--disable", action="store_true", help="停止并禁止每日定时任务")
    parser.add_argument("--enable", action="store_true", help="解除定时任务禁用标记")
    parser.add_argument("--stop", action="store_true", help="终止正在运行的调度进程")
    parser.add_argument("--status", action="store_true", help="查看定时任务状态")
    parser.add_argument(
        "--keywords",
        default=",".join(KEYWORDS),
        help="逗号分隔的监控关键词，默认全部",
    )
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    if args.disable:
        disable_scheduler()
        return 0
    if args.enable:
        enable_scheduler()
        return 0
    if args.stop:
        return 0 if stop_scheduler() else 1
    if args.status:
        pid = scheduler_running()
        state = f"运行中（pid {pid}）" if pid else "未运行"
        flag = "已禁用" if DISABLED_FLAG.exists() else "未禁用"
        log(f"[SCHED] 每日 08:00 定时任务：{state} · 启动标记：{flag}")
        return 0

    if DISABLED_FLAG.exists():
        log("[SCHED] 每日定时任务已在 .scheduler.off 中禁用，本次不启动")
        log("[SCHED] 如需恢复：scheduler_service.py --enable")
        return 0

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
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"[SCHED] 调度器已启动，每天 08:00（{TIMEZONE}）执行 {len(keywords)} 个关键词")
    log(f"[SCHED] 关键词：{', '.join(keywords)}")
    log(f"[SCHED] 下次运行：{next_run}")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log("[SCHED] 调度器已停止")
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
