"""一次完整情报 run：抓取 → RAG → 多 Agent → 预警暂存（不自动推送）。"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import config
import database
import fetcher
import notify
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


def run(
    keyword: str,
    sources: list[str],
    trigger: str = "manual",
    log=print,
) -> RunResult:
    run_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    database.start_run(run_id, trigger, keyword, sources)

    try:
        log(f"Agent 正在从 {len(sources)} 个数据源抓取最新资讯...")
        items = fetcher.fetch_all(sources, keyword, per_source=10, log=log)
        inserted = database.save_items(items)
        log(f"数据清洗完成，过滤 {max(len(items) - inserted, 0)} 条重复/冗余数据。")

        if not items:
            log("[WARN] 本次未抓取到新资讯，使用最近存档继续分析")
            df = database.fetch_recent(days=1, keyword=keyword)
            items = df.to_dict("records") if not df.empty else []

        if rag_store.count() == 0:
            rag_store.backfill(days=7, log=log)
        rag_store.index_items(items, log=log)

        titles = [str(item.get("title") or "") for item in items if item.get("title")]
        if not titles:
            df = database.fetch_recent(days=7, keyword=keyword)
            titles = df["title"].tolist() if not df.empty else []

        memory = rag_memory.build_context(keyword, titles, log=log)

        log(f"调用多 Agent 提取结构化特征（输入 {len(titles)} 条标题）...")
        analysis = graph.run(items, keyword, memory, run_id=run_id, log=log)
        analysis["_rag"] = {
            "similar": memory.similar[:6],
            "comparison": memory.comparison,
            "mode": memory.mode,
        }
        analysis["_duration_ms"] = int((time.perf_counter() - started) * 1000)

        alert_id: int | None = None
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
    result = run(args.keyword, sources, trigger=args.trigger, log=log)

    log(f"run_id={result.run_id} · 入库 {result.inserted} 条 · 模式 {result.analysis['_mode']}")
    log(f"预警任务：{result.alert_id}")

    if args.approve and result.alert_id:
        push_alert(result.alert_id, log=log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
