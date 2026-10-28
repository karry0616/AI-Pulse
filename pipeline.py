"""一次完整情报 run：抓取 → RAG → 多 Agent → 预警暂存（不自动推送）。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import database
import fetcher
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


class RunLocked(RuntimeError):
    """已有任务在运行，本次拒绝启动（防止重复扣费与并发写库）。"""


def compute_heat(items: list[dict]) -> int:
    """由资讯条数与命中源数计算热度（800-2000），不采信模型自报数值。"""
    sources = {str(item.get("source") or "") for item in items}
    sources.discard("")
    heat = 800 + 50 * min(len(items), 20) + 40 * min(len(sources), 5)
    return max(800, min(2000, heat))


def _read_lock() -> dict:
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lock_is_stale() -> bool:
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return True
    return age > LOCK_STALE_SECONDS


@contextmanager
def run_lock(keyword: str, trigger: str, log=print):
    """跨进程互斥：UI 手动、调度器、多个浏览器会话共用一把锁。"""
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_lock()
            if not _lock_is_stale():
                raise RunLocked(
                    f"已有任务正在运行（pid {holder.get('pid', '?')} · "
                    f"{holder.get('keyword', '?')} · {holder.get('trigger', '?')}），请稍后重试"
                )
            log(f"[LOCK] 清理残留锁（pid {holder.get('pid', '?')}）")
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
        try:
            yield
        finally:
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
            log("[LOCK] 运行锁已释放")
        return

    raise RunLocked("锁竞争失败，请稍后重试")


def run(
    keyword: str,
    sources: list[str],
    trigger: str = "manual",
    log=print,
) -> RunResult:
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

        if rag_store.count() == 0:
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

        analysis["_rag"] = {
            "similar": memory.similar[:6],
            "comparison": memory.comparison,
            "mode": memory.mode,
        }
        analysis["_duration_ms"] = int((time.perf_counter() - started) * 1000)

        updated = database.update_sentiments(analysis.get("_events_detail") or [], keyword)
        if updated:
            log(f"[SENTIMENT] 已用 Agent 判定回写 {updated} 条资讯情感")

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


def push_alert(alert_id: int, log=print) -> notify.PushResult:
    """人工审核通过后真正推送，并回写审批状态。"""
    alert = database.get_alert(alert_id)
    if not alert:
        log(f"[WARN] 预警 #{alert_id} 不存在，无法推送")
        return notify.PushResult(False, "Webhook", "预警不存在")

    if alert["status"] == "sent":
        log(f"[HITL] 预警 #{alert_id} 已推送过，跳过重复发送")
        return notify.PushResult(True, notify.channel_name(), "已发送")

    if alert["status"] not in ("pending", "failed"):
        log(f"[HITL] 预警 #{alert_id} 状态为 {alert['status']}，不可推送")
        return notify.PushResult(False, notify.channel_name(), alert["status"])

    result = notify.send_webhook(
        warning_text=alert["warning_text"],
        keyword=alert["keyword"],
        heat_score=alert["heat_score"],
        log=log,
    )
    database.mark_alert(
        alert_id,
        "sent" if result.ok else "failed",
        result.detail,
    )
    log(f"[HITL] 预警 #{alert_id} 审批完成，状态：{'sent' if result.ok else 'failed'}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse pipeline 自测")
    parser.add_argument("--keyword", default="AI Agent")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--approve", action="store_true", help="审核通过本次暂存的预警")
    parser.add_argument("--sources", default="TechCrunch,机器之心,36Kr,HackerNews")
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    def log(message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}")

    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    try:
        result = run(args.keyword, sources, trigger=args.trigger, log=log)
    except RunLocked as exc:
        log(f"[WARN] {exc}")
        return 1

    log(f"run_id={result.run_id} · 入库 {result.inserted} 条 · 模式 {result.analysis['_mode']}")
    log(f"预警任务：{result.alert_id}")

    if args.approve and result.alert_id:
        push_alert(result.alert_id, log=log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
