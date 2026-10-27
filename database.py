"""SQLite 持久化层：daily_intel 表的建表、写入与查询。

app.py / fetcher.py 只通过本模块访问数据库，SQL 不散落到别处。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent / "pulse.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_intel (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT,
    sentiment  TEXT,
    source     TEXT,
    url        TEXT,
    fetched_at TEXT,
    UNIQUE(date, title)
);

CREATE TABLE IF NOT EXISTS run_logs (
    run_id      TEXT PRIMARY KEY,
    trigger     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    sources     TEXT,
    items       INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    mode        TEXT,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT,
    node              TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    latency_ms        INTEGER DEFAULT 0,
    ok                INTEGER DEFAULT 1,
    error             TEXT,
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    level        TEXT,
    heat_score   INTEGER,
    warning_text TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT,
    reviewed_at  TEXT,
    sent_at      TEXT,
    push_result  TEXT
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表 + 索引，可重复调用。"""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intel_date ON daily_intel(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intel_keyword ON daily_intel(keyword)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_run ON llm_calls(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_run ON alerts(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_status ON alerts(status)")


def save_items(items: list[dict]) -> int:
    """批量写入，按 (date, title) 去重，返回实际新增条数。"""
    if not items:
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            item.get("date") or date.today().isoformat(),
            item.get("keyword") or "",
            item.get("title") or "",
            item.get("summary") or "",
            item.get("sentiment") or "neutral",
            item.get("source") or "",
            item.get("url") or "",
            item.get("fetched_at") or now,
        )
        for item in items
        if item.get("title")
    ]

    with _connect() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO daily_intel
                (date, keyword, title, summary, sentiment, source, url, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return conn.total_changes - before


def fetch_recent(days: int = 7, keyword: str | None = None) -> pd.DataFrame:
    """取最近 N 天（含今天）的原始情报，按日期倒序。"""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    sql = "SELECT * FROM daily_intel WHERE date >= ?"
    params: list = [since]

    if keyword:
        sql += " AND keyword = ?"
        params.append(keyword)

    sql += " ORDER BY date DESC, id DESC"

    with _connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def daily_stats(days: int = 7, keyword: str | None = None) -> pd.DataFrame:
    """按日期统计情感分布，列：date / positive / neutral / negative / total。"""
    columns = ["date", "positive", "neutral", "negative", "total"]
    df = fetch_recent(days=days, keyword=keyword)

    if df.empty:
        return pd.DataFrame(columns=columns)

    stats = (
        df.pivot_table(
            index="date",
            columns="sentiment",
            values="id",
            aggfunc="count",
            fill_value=0,
        )
        .rename_axis(None, axis=1)
        .reset_index()
    )

    for col in ("positive", "neutral", "negative"):
        if col not in stats.columns:
            stats[col] = 0

    stats = stats[columns[:-1]]
    stats["total"] = stats[["positive", "neutral", "negative"]].sum(axis=1)

    return stats.sort_values("date").reset_index(drop=True)


def history_cards(limit: int = 5) -> list[dict]:
    """侧边栏历史卡片：每天取条数最多的关键词，情感得分 = 正面占比。"""
    df = fetch_recent(days=30)
    if df.empty:
        return []

    grouped = (
        df.groupby(["date", "keyword"])["sentiment"]
        .agg(total="count", positive=lambda s: (s == "positive").sum())
        .reset_index()
    )
    grouped["score"] = (grouped["positive"] / grouped["total"] * 100).round().astype(int)

    daily = (
        grouped.sort_values(["date", "total"], ascending=[False, False])
        .drop_duplicates(subset=["date"], keep="first")
        .head(limit)
    )

    return [
        {
            "date": row["date"],
            "keyword": row["keyword"],
            "score": int(row["score"]),
            "count": int(row["total"]),
        }
        for _, row in daily.iterrows()
    ]


def latest_snapshot(keyword: str | None = None) -> dict:
    """今日基础统计，供 KPI 与日志使用。"""
    today = date.today().isoformat()
    df = fetch_recent(days=1, keyword=keyword)
    df = df[df["date"] == today] if not df.empty else df

    if df.empty:
        return {"total": 0, "positive": 0, "neutral": 0, "negative": 0, "negative_ratio": 0}

    total = len(df)
    positive = int((df["sentiment"] == "positive").sum())
    negative = int((df["sentiment"] == "negative").sum())

    return {
        "total": total,
        "positive": positive,
        "neutral": int((df["sentiment"] == "neutral").sum()),
        "negative": negative,
        "negative_ratio": round(negative / total * 100) if total else 0,
    }


# ---------------------------------------------------------------------------
# 运行记录 / LLM 成本 / 预警审批
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start_run(run_id: str, trigger: str, keyword: str, sources: list[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO run_logs
                (run_id, trigger, keyword, sources, status, started_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (run_id, trigger, keyword, ",".join(sources), _now()),
        )


def finish_run(
    run_id: str,
    status: str,
    items: int = 0,
    inserted: int = 0,
    mode: str = "",
    duration_ms: int = 0,
    error: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE run_logs
               SET status = ?, items = ?, inserted = ?, mode = ?,
                   duration_ms = ?, error = ?, finished_at = ?
             WHERE run_id = ?
            """,
            (status, items, inserted, mode, duration_ms, error, _now(), run_id),
        )


def log_llm_call(
    run_id: str,
    node: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    ok: bool = True,
    error: str = "",
) -> None:
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO llm_calls
                (run_id, node, model, prompt_tokens, completion_tokens,
                 total_tokens, latency_ms, ok, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                node,
                model,
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                total,
                int(latency_ms or 0),
                1 if ok else 0,
                error,
                _now(),
            ),
        )


def usage_summary(run_id: str) -> dict:
    """一次 run 的 token / 耗时汇总，供 UI 与调度日志展示。"""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)                              AS calls,
                   COALESCE(SUM(prompt_tokens), 0)       AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0)   AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)        AS total_tokens,
                   COALESCE(SUM(latency_ms), 0)          AS latency_ms
              FROM llm_calls
             WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    return dict(row) if row else {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "latency_ms": 0,
    }


def llm_calls_for_run(run_id: str) -> list[dict]:
    """一次 run 内各 Agent 节点的调用明细，供 UI 展示执行轨迹。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT node, model, prompt_tokens, completion_tokens,
                   total_tokens, latency_ms, ok, error
              FROM llm_calls
             WHERE run_id = ?
             ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_alert(
    run_id: str,
    keyword: str,
    warning_text: str,
    level: str = "LOW",
    heat_score: int | None = None,
) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO alerts
                (run_id, keyword, level, heat_score, warning_text, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (run_id, keyword, level, heat_score, warning_text, _now()),
        )
        return int(cursor.lastrowid)


def get_alert(alert_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return dict(row) if row else None


def latest_alert(run_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_pending_alert(keyword: str | None = None) -> dict | None:
    """刷新页面后仍能找到待审批的预警（Streamlit session 会重建）。"""
    sql = "SELECT * FROM alerts WHERE status = 'pending'"
    params: list = []
    if keyword:
        sql += " AND keyword = ?"
        params.append(keyword)
    sql += " ORDER BY id DESC LIMIT 1"

    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def mark_alert(alert_id: int, status: str, push_result: str = "") -> None:
    now = _now()
    with _connect() as conn:
        if status == "sent":
            conn.execute(
                "UPDATE alerts SET status = ?, reviewed_at = ?, sent_at = ?, push_result = ? WHERE id = ?",
                (status, now, now, push_result, alert_id),
            )
        elif status == "failed":
            conn.execute(
                "UPDATE alerts SET status = ?, reviewed_at = ?, push_result = ? WHERE id = ?",
                (status, now, push_result, alert_id),
            )
        else:
            conn.execute(
                "UPDATE alerts SET status = ?, reviewed_at = ? WHERE id = ?",
                (status, now, alert_id),
            )


def recent_runs(limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_logs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def negative_ratio_on(day: str, keyword: str | None = None) -> float | None:
    """指定日期的负面占比（0-100），无数据返回 None。"""
    sql = "SELECT sentiment, COUNT(*) AS n FROM daily_intel WHERE date = ?"
    params: list = [day]
    if keyword:
        sql += " AND keyword = ?"
        params.append(keyword)
    sql += " GROUP BY sentiment"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    counts = {row["sentiment"]: int(row["n"]) for row in rows}
    total = sum(counts.values())
    if not total:
        return None
    return round(counts.get("negative", 0) / total * 100, 1)
