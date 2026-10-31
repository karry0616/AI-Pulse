"""方案对比实验（ablation）：同一批真实资讯，三种方案各跑一遍。

    A · 规则模板     纯代码，无模型调用（产品里的 fallback 兜底）
    B · 单次 LLM     标题直接进 Analyst，一次调用出结构化 JSON，无 Collector / 无 Critic 重试
    C · 三 Agent     Collector → Analyst → Critic（≤3 回边），即线上链路

对比维度都是可机械核对、不依赖人工打分的指标：

    契约通过率            首次输出是否满足 JSON 契约（字段 / 范围 / 标题可核验）
    top_news 可核验率      标题条里能在输入标题中找到原文的比例（越低 = 幻觉越多）
    负面条数偏差 MAE       negative_alerts 与规则情感词典口径的绝对差
    成本 / 耗时            token 与毫秒

用法：
    .\\.venv\\Scripts\\python.exe eval\\baseline.py --limit 20 --keyword "AI Agent"
    .\\.venv\\Scripts\\python.exe eval\\baseline.py --group A          # 只跑规则组，不花钱

结果写入 docs/eval/baseline.json，并把 Markdown 表格打印到标准输出。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database  # noqa: E402
import pipeline  # noqa: E402
from agents import analyst, critic, graph  # noqa: E402
from agents.state import AgentState  # noqa: E402
from rag.memory import MemoryContext  # noqa: E402
from sentiment import guess_sentiment  # noqa: E402

MEMORY = MemoryContext(comparison="相比上周同期，该话题负面占比基本持平", mode="none")
OUTPUT = ROOT / "docs" / "eval" / "baseline.json"


def load_corpus(db_file: Path, keyword: str, limit: int) -> list[dict]:
    """从主库读一批真实资讯；只读，不写回。"""
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT title, summary, source, date, sentiment
              FROM daily_intel
             WHERE keyword = ? AND COALESCE(title, '') <> ''
             ORDER BY date DESC, id DESC
             LIMIT ?
            """,
            (keyword, limit),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def bullet_stats(text: str, titles: list[str]) -> tuple[int, int]:
    """返回 (条数, 可核验条数)。可核验 = 能在输入标题里找到原文。"""
    bullets = [line for line in str(text or "").splitlines() if line.strip().startswith("- ")]
    verified = 0
    for line in bullets:
        match = critic.BULLET_RE.match(line)
        if match and critic._matches_input_title(match.group(1), titles):
            verified += 1
    return len(bullets), verified


def score(draft: dict, state: AgentState) -> dict:
    """把一份草稿折算成可对比的指标。"""
    issues = critic.check_contract(draft, state)
    bullets, verified = bullet_stats(draft.get("top_news"), state.titles)
    lexicon_negative = sum(1 for title in state.titles if guess_sentiment(title) == "negative")
    try:
        alerts = int(str(draft.get("negative_alerts") or 0))
    except ValueError:
        alerts = 0
    return {
        "contract_pass": not issues,
        "issues": issues,
        "bullets": bullets,
        "verified_bullets": verified,
        "verified_ratio": round(verified / bullets, 3) if bullets else 0.0,
        "hallucinated": max(bullets - verified, 0),
        "lexicon_negative": lexicon_negative,
        "alert_error": abs(alerts - lexicon_negative),
    }


def run_group_a(items: list[dict], keyword: str) -> dict:
    state = AgentState(run_id="baseline-A", keyword=keyword, items=items, memory=MEMORY)
    started = time.perf_counter()
    draft = analyst.fallback(state, "baseline-A 规则模板", lambda _m: None)
    elapsed = int((time.perf_counter() - started) * 1000)
    result = score(draft, AgentState(run_id="a", keyword=keyword, items=items, memory=MEMORY))
    result.update({"latency_ms": elapsed, "total_tokens": 0, "calls": 0, "attempts": 1})
    return result


def run_group_b(items: list[dict], keyword: str, sample_id: str) -> dict:
    """单次 LLM 直出：Analyst 一次调用，不重试、不进 Critic 循环。"""
    state = AgentState(run_id=sample_id, keyword=keyword, items=items, memory=MEMORY)
    started = time.perf_counter()
    draft = analyst.run(state, lambda _m: None)
    elapsed = int((time.perf_counter() - started) * 1000)
    usage = database.usage_summary(sample_id)
    result = score(draft, state)
    result.update(
        {
            "latency_ms": elapsed,
            "total_tokens": usage["total_tokens"],
            "calls": usage["calls"],
            "attempts": 1,
            "degraded": state.mode == "fallback",
        }
    )
    return result


def run_group_c(items: list[dict], keyword: str, sample_id: str) -> dict:
    """线上链路：Collector → Analyst → Critic（≤3 回边）。"""
    started = time.perf_counter()
    analysis = graph.run(items, keyword, MEMORY, run_id=sample_id, log=lambda _m: None)
    elapsed = int((time.perf_counter() - started) * 1000)
    usage = database.usage_summary(sample_id)
    state = AgentState(run_id=sample_id, keyword=keyword, items=items, memory=MEMORY)
    result = score(analysis, state)
    result.update(
        {
            "latency_ms": elapsed,
            "total_tokens": usage["total_tokens"],
            "calls": usage["calls"],
            "attempts": analysis.get("_attempts", 1),
            "rejections": analysis.get("_rejections", 0),
            "degraded": analysis.get("_mode") == "fallback",
        }
    )
    return result


def run_group_p(
    fresh: list[dict], archive: list[dict], keyword: str, sample_id: str
) -> dict:
    """修复后场景：新增资讯不足时先用存档补齐，再走三 Agent 链路。

    用来验证「补档」这条改动是否真的把单条场景的契约通过率拉起来。
    """
    padded = pipeline.pad_analysis_items(fresh, archive)
    started = time.perf_counter()
    analysis = graph.run(padded, keyword, MEMORY, run_id=sample_id, log=lambda _m: None)
    elapsed = int((time.perf_counter() - started) * 1000)
    usage = database.usage_summary(sample_id)
    state = AgentState(run_id=sample_id, keyword=keyword, items=padded, memory=MEMORY)
    result = score(analysis, state)
    result.update(
        {
            "latency_ms": elapsed,
            "total_tokens": usage["total_tokens"],
            "calls": usage["calls"],
            "attempts": analysis.get("_attempts", 1),
            "rejections": analysis.get("_rejections", 0),
            "degraded": analysis.get("_mode") == "fallback",
            "padded_items": len(padded),
        }
    )
    return result


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"samples": 0}
    n = len(rows)
    return {
        "samples": n,
        "contract_pass_rate": round(sum(1 for r in rows if r["contract_pass"]) / n * 100, 1),
        "avg_verified_ratio": round(sum(r["verified_ratio"] for r in rows) / n, 3),
        "hallucination_rate": round(sum(1 for r in rows if r["hallucinated"]) / n * 100, 1),
        "avg_alert_error": round(sum(r["alert_error"] for r in rows) / n, 2),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "avg_attempts": round(sum(r["attempts"] for r in rows) / n, 2),
        "degraded_rate": round(sum(1 for r in rows if r.get("degraded")) / n * 100, 1),
    }


def make_batches(corpus: list[dict], size: int, min_size: int = 3) -> list[list[dict]]:
    """按真实使用场景切批：一次 run 分析一批资讯，不是单条分析。

    Critic 契约要求 top_news ≥3 条可核验标题，单条样本天然不达标，
    因此对比必须以「批」为单位，否则量到的是实验室噪音而不是链路差异。
    """
    batches = [corpus[i : i + size] for i in range(0, len(corpus), size)]
    return [batch for batch in batches if len(batch) >= min_size]


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse 方案对比实验")
    parser.add_argument("--keyword", default="AI Agent")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch", type=int, default=6, help="每批资讯条数（一次 run 的规模）")
    parser.add_argument(
        "--min-batch", type=int, default=3, help="少于该条数的尾部批次丢弃；压测时设为 1"
    )
    parser.add_argument("--db", default=str(ROOT / "pulse.db"))
    parser.add_argument("--groups", default="ABC", help="要跑的分组，如 AB / C")
    parser.add_argument("--out", default=str(OUTPUT))
    args = parser.parse_args()

    config.load_env()
    corpus = load_corpus(Path(args.db), args.keyword, args.limit)
    if not corpus:
        print(f"[WARN] 主库里没有「{args.keyword}」的数据，先跑一次抓取再对比")
        return 1

    # LLM 调用记录写到临时库，避免污染生产库的成本统计
    database.DB_PATH = Path(args.out).parent / "baseline_calls.db"
    database.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    database.init_db()

    batches = make_batches(corpus, args.batch, args.min_batch)
    if not batches:
        print(f"[WARN] 语料只有 {len(corpus)} 条，不足一批（≥3 条）")
        return 1

    print(
        f"[EVAL] 语料 {len(corpus)} 条 → {len(batches)} 批（每批 {args.batch} 条）· "
        f"关键词「{args.keyword}」· 分组 {args.groups}"
    )

    groups: dict[str, list[dict]] = {}
    for name in args.groups:
        rows: list[dict] = []
        for index, batch in enumerate(batches, start=1):
            sample_id = f"bl-{name}-{index}-{datetime.now():%H%M%S}"
            try:
                if name == "A":
                    row = run_group_a(batch, args.keyword)
                elif name == "B":
                    row = run_group_b(batch, args.keyword, sample_id)
                elif name == "C":
                    row = run_group_c(batch, args.keyword, sample_id)
                elif name == "P":
                    archive = [item for item in corpus if item not in batch]
                    row = run_group_p(batch, archive, args.keyword, sample_id)
                else:
                    raise ValueError(f"未知分组：{name}")
            except Exception as exc:  # 单批失败不影响整组
                row = {
                    "contract_pass": False,
                    "issues": [f"{type(exc).__name__}: {exc}"],
                    "bullets": 0,
                    "verified_bullets": 0,
                    "verified_ratio": 0.0,
                    "hallucinated": 0,
                    "lexicon_negative": 0,
                    "alert_error": 0,
                    "latency_ms": 0,
                    "total_tokens": 0,
                    "calls": 0,
                    "attempts": 0,
                    "degraded": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            row["batch"] = index
            row["items"] = len(batch)
            row["titles"] = [item["title"] for item in batch]
            rows.append(row)
            print(
                f"  [{name}] 批 {index}/{len(batches)} · {len(batch)} 条 "
                f"pass={row['contract_pass']} verify={row['verified_ratio']:.2f} "
                f"tok={row['total_tokens']} {row['latency_ms']}ms"
            )
        groups[name] = rows

    summary = {name: aggregate(rows) for name, rows in groups.items()}
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "corpus_size": len(corpus),
        "batch_size": args.batch,
        "summary": summary,
        "samples": groups,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n| 方案 | 契约通过率 | top_news 可核验率 | 有幻觉批次占比 | 负面条数 MAE | 平均尝试 | 平均耗时 | token |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    labels = {
        "A": "A · 规则模板",
        "B": "B · 单次 LLM",
        "C": "C · 三 Agent",
        "P": "P · 补档后三 Agent",
    }
    for name, item in summary.items():
        print(
            f"| {labels.get(name, name)} | {item['contract_pass_rate']}% | "
            f"{item['avg_verified_ratio']:.2f} | {item['hallucination_rate']}% | "
            f"{item['avg_alert_error']} | {item['avg_attempts']} | "
            f"{item['avg_latency_ms']} ms | {item['total_tokens']} |"
        )
    print(f"\n[EVAL] 明细已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
