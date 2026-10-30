"""一次完整情报 run：抓取 → RAG → 多 Agent → 预警暂存（不自动推送）。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import database
import events
import fetcher
import logging_setup
import notify
import reports
from agents import graph
from rag import memory as rag_memory
from rag import store as rag_store


@dataclass
class RunResult:
    run_id: str
    keyword: str
    analysis: dict | None = None
    alert_id: int | None = None
    items: int = 0
    inserted: int = 0
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    status: str = "success"
    error: str = ""


LOCK_PATH = Path(__file__).resolve().parent / ".pulse.lock"
LOCK_STALE_SECONDS = 30 * 60  # 超过 30 分钟视为异常残留，可被抢占


class RunLockedError(RuntimeError):
    """已有任务在运行，本次拒绝启动（防止重复扣费与并发写库）。"""


DEFAULT_COOLDOWN_SECONDS = 90


def cooldown_window() -> int:
    """按钮冷却窗口（秒）；.env 里 RUN_COOLDOWN_SECONDS=0 可关闭。"""
    config.load_env()
    raw = os.getenv("RUN_COOLDOWN_SECONDS", "").strip()
    if not raw:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS


def cooldown_remaining(keyword: str, window: int | None = None) -> int:
    """距上次成功 run 还需等待的秒数；0 表示可立即执行。"""
    limit = cooldown_window() if window is None else window
    if limit <= 0:
        return 0

    row = database.latest_success_run(keyword)
    finished = (row or {}).get("finished_at")
    if not finished:
        return 0
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(finished)).total_seconds()
    except ValueError:
        return 0
    return max(int(limit - elapsed), 0)


def _tee_log(log):
    """把同一份日志同时写给调用方（UI 面板 / 测试）与落盘 logger。

    调用方本身已落盘时（如 API 的 _log）直接返回，避免同一行写两遍。
    """
    if getattr(log, "writes_file", False):
        return log

    def emit(message: str) -> None:
        log(message)
        logging_setup.log(message)

    emit.writes_file = True
    return emit


def compute_heat(items: list[dict]) -> int:
    """由资讯条数、命中源数与极端规模计算热度（800-2000），不采信模型自报数值。

    旧公式 50 × min(条数, 20) + 40 × min(源数, 5) 在 7 源 × 10 条时必然打满
    2000，KPI 失去区分度；新公式改为三段递增：常规量级留出差异空间，
    只有极端规模（200 条 / 6+ 源）才接近上限。
    """
    sources = {str(item.get("source") or "") for item in items}
    sources.discard("")
    volume = min(len(items), 40) / 40
    breadth = min(len(sources), 6) / 6
    extreme = min(len(items), 200) / 200
    heat = 800 + round(700 * volume + 300 * breadth + 200 * extreme)
    return max(800, min(2000, heat))


def compute_positive_ratio(items: list[dict]) -> int | None:
    """按抓取/Agent 情感统计正面占比，用于校正模型的 positive_ratio。"""
    if not items:
        return None
    positive = sum(1 for item in items if str(item.get("sentiment") or "") == "positive")
    return round(positive / len(items) * 100)


def _ratio_percent(text) -> float | None:
    raw = str(text or "").strip().rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return None


def _read_lock() -> dict:
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lock_age() -> float:
    try:
        return time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return float("inf")


def _lock_is_stale() -> bool:
    return _lock_age() > LOCK_STALE_SECONDS


def pid_alive(pid: int) -> bool:
    """判断进程是否仍存活（Windows 用 OpenProcess，避免 os.kill 误杀）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    process_query_limited = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def lock_status() -> dict | None:
    """当前运行锁的持有者与存活状态；无锁返回 None。"""
    if not LOCK_PATH.exists():
        return None
    holder = _read_lock()
    pid = holder.get("pid")
    alive = pid_alive(pid) if isinstance(pid, int) else False
    return {
        **holder,
        "alive": alive,
        "stale": _lock_is_stale(),
        "age_seconds": int(max(_lock_age(), 0)),
        "self": pid == os.getpid(),
    }


def force_stop(log=print) -> dict:
    """强制终止持有锁的任务并清理锁文件。

    进程已死或超时 → 直接释放；进程仍存活且不是当前进程 → 终止该进程再释放；
    若正是当前进程（UI 线程内任务）则只能释放锁，无法安全杀掉自身。
    """
    status = lock_status()
    if status is None:
        return {"released": False, "killed": False, "reason": "当前没有运行中的任务"}

    pid = status.get("pid")
    killed = False
    reason = "已释放残留锁"

    if isinstance(pid, int) and pid > 0 and status["alive"] and pid != os.getpid():
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        killed = True
        reason = f"已终止任务进程 pid {pid} 并释放锁"
    elif status["self"]:
        reason = "锁由当前页面进程持有：已释放锁，但执行线程无法强制中断，请等待本轮结束"

    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    log(f"[LOCK] 强制终止：{reason}")
    return {"released": True, "killed": killed, "pid": pid, "reason": reason}


@contextmanager
def run_lock(keyword: str, trigger: str, log=print):
    """跨进程互斥：UI 手动、调度器、多个浏览器会话共用一把锁。

    锁文件存在时先看持有进程是否还活着：进程已退出则立即接管，
    不再等 30 分钟超时，避免「有任务在进行中」但实际没人跑的卡死。
    """
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_lock()
            pid = holder.get("pid")
            alive = pid_alive(pid) if isinstance(pid, int) else False
            if alive and not _lock_is_stale():
                raise RunLockedError(
                    f"已有任务正在运行（pid {pid or '?'} · "
                    f"{holder.get('keyword', '?')} · {holder.get('trigger', '?')}），"
                    f"可在页面点击「强制终止任务」释放"
                )
            why = "进程已退出" if not alive else f"超过 {LOCK_STALE_SECONDS // 60} 分钟未更新"
            log(f"[LOCK] 接管残留锁（pid {pid or '?'} · {why}）")
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "pid": os.getpid(),
                    "keyword": keyword,
                    "trigger": trigger,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                },
                fh,
                ensure_ascii=False,
            )
        log(f"[LOCK] 已获取运行锁（pid {os.getpid()}）")
        orphans = database.close_orphan_runs()
        if orphans:
            log(f"[STATE] 已将 {orphans} 条异常中断的运行记录标记为 failed")
        try:
            yield
        finally:
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
            log("[LOCK] 运行锁已释放")
        return

    raise RunLockedError("锁竞争失败，请稍后重试")


def run(
    keyword: str,
    sources: list[str],
    trigger: str = "manual",
    log=print,
) -> RunResult:
    log = _tee_log(log)
    with run_lock(keyword, trigger, log=log):
        return _run_once(keyword, sources, trigger, log)


def _run_once(
    keyword: str,
    sources: list[str],
    trigger: str,
    log,
) -> RunResult:
    run_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()

    expired = database.expire_stale_alerts()
    if expired:
        log(f"[HITL] 已过期 {expired} 条超过 24 小时未审核的预警")

    database.start_run(run_id, trigger, keyword, sources)

    try:
        log(f"Agent 正在从 {len(sources)} 个数据源抓取最新资讯...")
        items = fetcher.fetch_all(sources, keyword, per_source=10, log=log)
        fresh = database.filter_unseen(items)
        inserted = database.save_items(items)
        log(f"数据清洗完成，过滤 {max(len(items) - inserted, 0)} 条重复/冗余数据。")

        if len(items) > len(fresh):
            log(f"[INCR] 跳过 {len(items) - len(fresh)} 条已入库资讯，仅分析新增内容")

        analysis_items = fresh
        if not analysis_items:
            log("[WARN] 本次未抓取到新资讯，使用最近存档继续分析")
            df = database.fetch_recent(days=1, keyword=keyword)
            analysis_items = df.to_dict("records") if not df.empty else []
            if not analysis_items:
                analysis_items = items

        if rag_store.available() and rag_store.count() == 0:
            rag_store.backfill(days=7, log=log)
        rag_store.index_items(items, log=log)

        titles = [str(item.get("title") or "") for item in analysis_items if item.get("title")]
        if not titles:
            df = database.fetch_recent(days=7, keyword=keyword)
            titles = df["title"].tolist() if not df.empty else []

        memory = rag_memory.build_context(keyword, titles, log=log)

        log(f"调用多 Agent 提取结构化特征（输入 {len(titles)} 条标题）...")
        analysis = graph.run(analysis_items, keyword, memory, run_id=run_id, log=log)

        model_heat = analysis.get("heat_score")
        analysis["heat_score"] = compute_heat(analysis_items)
        if model_heat != analysis["heat_score"]:
            log(
                f"[GUARD] heat_score 以代码计算为准：{analysis['heat_score']}"
                f"（模型值 {model_heat}）"
            )

        actual_ratio = compute_positive_ratio(analysis_items)
        model_ratio = _ratio_percent(analysis.get("positive_ratio"))
        if (
            actual_ratio is not None
            and model_ratio is not None
            and abs(model_ratio - actual_ratio) >= 20
        ):
            log(
                f"[GUARD] positive_ratio 与样本偏差过大，改用代码统计：{actual_ratio}%"
                f"（模型值 {analysis['positive_ratio']}）"
            )
            analysis["positive_ratio"] = f"{actual_ratio}%"

        analysis["_rag"] = {
            "similar": memory.similar[:6],
            "comparison": memory.comparison,
            "mode": memory.mode,
        }
        analysis["_duration_ms"] = int((time.perf_counter() - started) * 1000)

        updated = database.update_sentiments(analysis.get("_events_detail") or [], keyword)
        if updated:
            log(f"[SENTIMENT] 已用 Agent 判定回写 {updated} 条资讯情感")

        analysis["_event_threads"] = events.link_run(analysis_items, analysis, keyword, log=log)

        try:
            database.save_analysis(run_id, keyword, analysis)
            log("[STATE] 分析结果已落库，刷新页面仍可回放")
        except Exception as exc:  # 落库失败不影响本次结果展示
            log(f"[WARN] 分析结果落库失败：{type(exc).__name__}: {exc}")

        alert_id: int | None = None
        level = ""
        if analysis["negative_alerts"] > 0:
            level = "HIGH" if analysis["negative_alerts"] >= 2 else "MEDIUM"
            alert_id = database.save_alert(
                run_id=run_id,
                keyword=keyword,
                warning_text=analysis["warning_text"],
                level=level,
                heat_score=analysis["heat_score"],
            )
            log(f"[HITL] 预警已暂存（#{alert_id}），等待人工审核后推送。")
        else:
            log("未触发敏感词预警，无需生成人工审批任务。")

        usage = database.usage_summary(run_id)
        if usage["calls"]:
            log(
                f"[COST] 本次累计 {usage['total_tokens']} tok · "
                f"{usage['latency_ms'] / 1000:.1f}s · {usage['calls']} 次调用"
            )

        duration_ms = int((time.perf_counter() - started) * 1000)

        try:
            report_path = reports.save(
                analysis,
                keyword,
                run_id,
                items=len(items),
                inserted=inserted,
                usage=usage,
                alert_id=alert_id,
                level=level,
            )
            log(f"[REPORT] 已导出日报：{report_path.name}")
        except OSError as exc:
            log(f"[WARN] 日报导出失败：{type(exc).__name__}: {exc}")

        database.finish_run(
            run_id,
            status="success",
            items=len(items),
            inserted=inserted,
            mode=analysis["_mode"],
            duration_ms=duration_ms,
        )
        return RunResult(
            run_id=run_id,
            keyword=keyword,
            analysis=analysis,
            alert_id=alert_id,
            items=len(items),
            inserted=inserted,
            usage=usage,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        database.finish_run(
            run_id,
            status="failed",
            duration_ms=duration_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
        log(f"[WARN] pipeline 失败：{type(exc).__name__}: {exc}")
        raise


def push_alert(
    alert_id: int,
    log=print,
    reviewed_by: str = "operator",
    review_note: str = "",
) -> notify.PushResult:
    """人工审核通过后真正推送，并回写审批状态与审批人审计信息。"""
    alert = database.get_alert(alert_id)
    if not alert:
        log(f"[WARN] 预警 #{alert_id} 不存在，无法推送")
        return notify.PushResult(False, "Webhook", "预警不存在")

    if alert["status"] == "sent":
        log(f"[HITL] 预警 #{alert_id} 已推送过，跳过重复发送")
        return notify.PushResult(True, notify.channel_name(), "已发送")

    if alert["status"] not in ("pending", "failed", "rejected", "sending"):
        log(f"[HITL] 预警 #{alert_id} 状态为 {alert['status']}，不可推送")
        return notify.PushResult(False, notify.channel_name(), alert["status"])

    if alert["status"] == "rejected":
        log(f"[HITL] 预警 #{alert_id} 已被驳回，如需再次推送请重新审批")
        return notify.PushResult(False, notify.channel_name(), "已驳回")

    if not database.claim_alert(alert_id, reviewed_by, review_note):
        latest = database.get_alert(alert_id) or {}
        if latest.get("status") == "sent":
            log(f"[HITL] 预警 #{alert_id} 已由其他审批人推送，跳过重复发送")
            return notify.PushResult(True, notify.channel_name(), "已发送")
        log(f"[HITL] 预警 #{alert_id} 正被其他审批请求处理，放弃本次推送")
        return notify.PushResult(
            False, notify.channel_name(), "该预警正在被其他审批处理，请稍后刷新"
        )

    result = notify.send_webhook(
        warning_text=alert["warning_text"],
        keyword=alert["keyword"],
        heat_score=alert["heat_score"],
        alert_id=alert_id,
        log=log,
    )
    database.mark_alert(
        alert_id,
        "sent" if result.ok else "failed",
        result.detail,
        reviewed_by=reviewed_by,
        review_note=review_note,
    )
    log(
        f"[HITL] 预警 #{alert_id} 审批完成（{reviewed_by or '未署名'}），"
        f"状态：{'sent' if result.ok else 'failed'}"
    )
    return result


def reject_alert(
    alert_id: int,
    log=print,
    reviewed_by: str = "operator",
    review_note: str = "",
) -> dict:
    """驳回预警：不推送，只留审计记录。"""
    alert = database.get_alert(alert_id)
    if not alert:
        log(f"[WARN] 预警 #{alert_id} 不存在，无法驳回")
        return {"ok": False, "reason": "预警不存在"}

    if alert["status"] == "sent":
        log(f"[WARN] 预警 #{alert_id} 已推送，不能再驳回")
        return {"ok": False, "reason": "已推送，不可驳回"}

    if alert["status"] == "sending":
        log(f"[WARN] 预警 #{alert_id} 正在推送中，无法驳回")
        return {"ok": False, "reason": "正在推送中，请稍后刷新"}

    database.mark_alert(
        alert_id, "rejected", "", reviewed_by=reviewed_by, review_note=review_note
    )
    log(f"[HITL] 预警 #{alert_id} 已驳回（{reviewed_by or '未署名'}）")
    return {"ok": True, "reason": "已驳回", "alert_id": alert_id}


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse pipeline 自测")
    parser.add_argument("--keyword", default="AI Agent")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--approve", action="store_true", help="审核通过本次暂存的预警")
    parser.add_argument("--reviewer", default="cli", help="审批人署名（写入审计字段）")
    parser.add_argument("--sources", default="TechCrunch,机器之心,36Kr,HackerNews")
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    def log(message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}")

    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    try:
        result = run(args.keyword, sources, trigger=args.trigger, log=log)
    except RunLockedError as exc:
        log(f"[WARN] {exc}")
        return 1

    log(f"run_id={result.run_id} · 入库 {result.inserted} 条 · 模式 {result.analysis['_mode']}")
    log(f"预警任务：{result.alert_id}")

    if args.approve and result.alert_id:
        push_alert(result.alert_id, log=log, reviewed_by=args.reviewer)

    return 0


if __name__ == "__main__":
    sys.exit(main())
