"""SQLite 持久化层：daily_intel 表的建表、写入与查询。

app.py / fetcher.py 只通过本模块访问数据库，SQL 不散落到别处。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent
# 容器部署时用 PULSE_DB_PATH 指向挂载卷；本地默认仍在项目根目录
DB_PATH = Path(os.getenv("PULSE_DB_PATH") or (_ROOT / "pulse.db"))

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
    UNIQUE(date, title, keyword)
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
    push_result  TEXT,
    reviewed_by  TEXT,
    review_note  TEXT
);

CREATE TABLE IF NOT EXISTS event_threads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key       TEXT NOT NULL UNIQUE,
    keyword          TEXT NOT NULL,
    entity           TEXT,
    title            TEXT NOT NULL,
    first_date       TEXT,
    last_date        TEXT,
    item_count       INTEGER DEFAULT 0,
    latest_sentiment TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS event_links (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    date      TEXT,
    title     TEXT NOT NULL,
    source    TEXT,
    sentiment TEXT,
    created_at TEXT,
    UNIQUE(thread_id, title)
);

CREATE TABLE IF NOT EXISTS analyses (
    run_id     TEXT PRIMARY KEY,
    keyword    TEXT NOT NULL,
    created_at TEXT,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_intel_archive (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT,
    sentiment    TEXT,
    source       TEXT,
    url          TEXT,
    fetched_at   TEXT,
    archived_at  TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls_archive (
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
    created_at        TEXT,
    archived_at       TEXT
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


def _migrate_daily_intel_unique(conn: sqlite3.Connection) -> None:
    """v1：唯一键由 (date, title) 扩为 (date, title, keyword)。

    同一条新闻可能同时命中多个关键词，旧唯一键会让后写入的关键词丢失。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_intel'"
    ).fetchone()
    if not row or not row[0]:
        return

    normalized = "".join(row[0].split()).lower()
    if "unique(date,title,keyword)" in normalized:
        return

    conn.executescript(
        """
        CREATE TABLE daily_intel_v1 (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT NOT NULL,
            keyword    TEXT NOT NULL,
            title      TEXT NOT NULL,
            summary    TEXT,
            sentiment  TEXT,
            source     TEXT,
            url        TEXT,
            fetched_at TEXT,
            UNIQUE(date, title, keyword)
        );
        INSERT OR IGNORE INTO daily_intel_v1
            (id, date, keyword, title, summary, sentiment, source, url, fetched_at)
        SELECT id, date, keyword, title, summary, sentiment, source, url, fetched_at
          FROM daily_intel;
        DROP TABLE daily_intel;
        ALTER TABLE daily_intel_v1 RENAME TO daily_intel;
        """
    )


def _migrate_v3_review_columns(conn: sqlite3.Connection) -> None:
    """v3：alerts 增加审批人审计字段（老库需 ALTER，新库由 SCHEMA 直接建出）。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "reviewed_by" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN reviewed_by TEXT")
    if "review_note" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN review_note TEXT")


def init_db() -> None:
    """建表 + 迁移 + 索引，可重复调用。"""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            _migrate_daily_intel_unique(conn)
            conn.execute("PRAGMA user_version = 1")
        if version < 2:
            # v2 只新增事件线两张表（SCHEMA 已建），无需搬数据
            conn.execute("PRAGMA user_version = 2")
        if version < 3:
            # v3：审批审计字段 + 分析结果持久化表 + 归档表（后两张 SCHEMA 已建）
            _migrate_v3_review_columns(conn)
            conn.execute("PRAGMA user_version = 3")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intel_date ON daily_intel(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intel_keyword ON daily_intel(keyword)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intel_title ON daily_intel(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_run ON llm_calls(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_run ON alerts(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_status ON alerts(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_kw ON analyses(keyword, created_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_kw_last ON event_threads(keyword, last_date)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_link_thread ON event_links(thread_id)")


def save_items(items: list[dict]) -> int:
    """批量写入，按 (date, title, keyword) 去重，返回实际新增条数。"""
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


def filter_unseen(items: list[dict]) -> list[dict]:
    """只保留尚未入库的条目（按 date + title + keyword 判定），保持原顺序。"""
    if not items:
        return []

    days = {item.get("date") or date.today().isoformat() for item in items}
    placeholders = ",".join("?" for _ in days)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT date, keyword, title FROM daily_intel WHERE date IN ({placeholders})",
            list(days),
        ).fetchall()

    seen = {(row["date"], row["keyword"], row["title"]) for row in rows}
    fresh: list[dict] = []
    for item in items:
        title = item.get("title") or ""
        if not title:
            continue
        key = (item.get("date") or date.today().isoformat(), item.get("keyword") or "", title)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


def _norm_title(text: str) -> str:
    """去空白与标点、统一大小写，用于跨来源标题对齐。"""
    return re.sub(r"[^\w]+", "", str(text or "")).lower()


def update_sentiments(events: list[dict], keyword: str) -> int:
    """用 Agent 判定的事件情感回写同关键词资讯，返回实际更新的行数。

    Agent 只返回标题与情感，标题可能被轻微改写，因此按标题包含关系对齐。
    """
    updates: list[tuple[str, int]] = []
    for event in events or []:
        sentiment = str(event.get("sentiment") or "").lower()
        title = _norm_title(event.get("title"))
        if sentiment not in ("positive", "neutral", "negative") or not title:
            continue
        updates.append((sentiment, title))
    if not updates:
        return 0

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title FROM daily_intel WHERE keyword = ?", (keyword,)
        ).fetchall()
        lookup = {_norm_title(row["title"]): row["id"] for row in rows}

        pairs: list[tuple[str, int]] = []
        for sentiment, title in updates:
            row_id = lookup.get(title)
            if row_id is None:
                row_id = next(
                    (
                        candidate_id
                        for candidate, candidate_id in lookup.items()
                        if candidate and (title in candidate or candidate in title)
                    ),
                    None,
                )
            if row_id is not None:
                pairs.append((sentiment, row_id))

        if not pairs:
            return 0

        before = conn.total_changes
        conn.executemany("UPDATE daily_intel SET sentiment = ? WHERE id = ?", pairs)
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


def close_orphan_runs() -> int:
    """持锁后仍标记为 running 的记录必然是异常中断，统一收敛为 failed。"""
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE run_logs
               SET status = 'failed',
                   error = '进程异常中断，任务未正常结束',
                   finished_at = ?
             WHERE status = 'running'
            """,
            (_now(),),
        )
        return cursor.rowcount


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
    """写入待审批预警；同 keyword 当天已有待审/失败任务时复用并更新内容。"""
    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT id FROM alerts
             WHERE keyword = ? AND status IN ('pending', 'failed')
               AND date(created_at) = date('now', 'localtime')
             ORDER BY id DESC LIMIT 1
            """,
            (keyword,),
        ).fetchone()

        if existing:
            alert_id = int(existing["id"])
            conn.execute(
                """
                UPDATE alerts
                   SET run_id = ?, level = ?, heat_score = ?, warning_text = ?,
                       status = 'pending'
                 WHERE id = ?
                """,
                (run_id, level, heat_score, warning_text, alert_id),
            )
            return alert_id

        cursor = conn.execute(
            """
            INSERT INTO alerts
                (run_id, keyword, level, heat_score, warning_text, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (run_id, keyword, level, heat_score, warning_text, _now()),
        )
        return int(cursor.lastrowid)


def expire_stale_alerts(hours: int = 24) -> int:
    """把超过 N 小时仍无人审核的 pending 预警标记为 expired，返回条数。"""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE alerts SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
            (cutoff,),
        )
        return int(cursor.rowcount or 0)



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


def mark_alert(
    alert_id: int,
    status: str,
    push_result: str = "",
    reviewed_by: str = "",
    review_note: str = "",
) -> None:
    """回写审批状态与审计信息：谁、什么时候、以什么理由处理了这条预警。"""
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE alerts
               SET status = ?, reviewed_at = ?,
                   sent_at     = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                   push_result = CASE WHEN ? <> '' THEN ? ELSE push_result END,
                   reviewed_by = CASE WHEN ? <> '' THEN ? ELSE reviewed_by END,
                   review_note = CASE WHEN ? <> '' THEN ? ELSE review_note END
             WHERE id = ?
            """,
            (
                status, now,
                status, now,
                push_result, push_result,
                reviewed_by, reviewed_by,
                review_note, review_note,
                alert_id,
            ),
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


# ---------------------------------------------------------------------------
# 事件线与实体追踪
# ---------------------------------------------------------------------------
def recent_threads(keyword: str, days: int = 7, limit: int = 200) -> list[dict]:
    """取近 N 天仍在更新的同关键词事件线，供归并时匹配。"""
    since = (date.today() - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM event_threads
             WHERE keyword = ? AND COALESCE(last_date, first_date, '') >= ?
             ORDER BY last_date DESC, id DESC
             LIMIT ?
            """,
            (keyword, since, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_thread(
    thread_key: str,
    keyword: str,
    title: str,
    day: str,
    entity: str = "",
    sentiment: str = "neutral",
) -> int:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM event_threads WHERE thread_key = ?", (thread_key,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE event_threads SET last_date = ?, updated_at = ? WHERE id = ?",
                (day, _now(), int(existing["id"])),
            )
            return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO event_threads
                (thread_key, keyword, entity, title, first_date, last_date,
                 item_count, latest_sentiment, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (thread_key, keyword, entity, title, day, day, sentiment, _now()),
        )
        return int(cursor.lastrowid)


def touch_thread(
    thread_id: int,
    day: str,
    sentiment: str,
    entity: str = "",
) -> None:
    """事件线有新报道时推进 last_date 与计数。"""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE event_threads
               SET last_date = CASE WHEN COALESCE(last_date, '') > ? THEN last_date ELSE ? END,
                   item_count = item_count + 1,
                   latest_sentiment = ?,
                   entity = CASE WHEN COALESCE(entity, '') = '' THEN ? ELSE entity END,
                   updated_at = ?
             WHERE id = ?
            """,
            (day, day, sentiment, entity, _now(), thread_id),
        )


def link_thread_item(thread_id: int, item: dict) -> bool:
    """把一条资讯挂到事件线；重复标题返回 False，避免重复计数。"""
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO event_links
                (thread_id, date, title, source, sentiment, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                str(item.get("date") or date.today().isoformat()),
                str(item.get("title") or ""),
                str(item.get("source") or ""),
                str(item.get("sentiment") or "neutral"),
                _now(),
            ),
        )
        return bool(cursor.rowcount)


def top_threads(keyword: str | None = None, days: int = 7, limit: int = 5) -> list[dict]:
    """按报道数排序的事件线，供页面与日报展示。"""
    since = (date.today() - timedelta(days=days)).isoformat()
    sql = """
        SELECT id, entity, title, first_date, last_date, item_count, latest_sentiment
          FROM event_threads
         WHERE COALESCE(last_date, first_date, '') >= ?
    """
    params: list = [since]
    if keyword:
        sql += " AND keyword = ?"
        params.append(keyword)
    sql += " ORDER BY item_count DESC, last_date DESC, id DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def thread_timeline(thread_id: int, limit: int = 20) -> list[dict]:
    """一条事件线内按日期倒序的报道明细。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT date, title, source, sentiment
              FROM event_links
             WHERE thread_id = ?
             ORDER BY date DESC, id DESC
             LIMIT ?
            """,
            (thread_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 分析结果持久化（刷新不丢）与数据归档留存
# ---------------------------------------------------------------------------
def save_analysis(run_id: str, keyword: str, analysis: dict) -> None:
    """把一次 run 的结构化分析落库，页面刷新或重启后仍能回放。"""
    payload = json.dumps(analysis, ensure_ascii=False, default=str)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO analyses (run_id, keyword, created_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET payload = excluded.payload,
                                              created_at = excluded.created_at
            """,
            (run_id, keyword, _now(), payload),
        )


def load_analysis(keyword: str | None = None) -> dict | None:
    """取指定关键词（不传则全局）最近一次分析结果。"""
    sql = "SELECT payload FROM analyses"
    params: list = []
    if keyword:
        sql += " WHERE keyword = ?"
        params.append(keyword)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"

    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def latest_success_run(keyword: str) -> dict | None:
    """该关键词最近一次成功的 run，供按钮冷却判断。"""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM run_logs
             WHERE keyword = ? AND status = 'success'
             ORDER BY COALESCE(finished_at, started_at) DESC
             LIMIT 1
            """,
            (keyword,),
        ).fetchone()
    return dict(row) if row else None


def archive_old(days: int = 90, dry_run: bool = False) -> dict:
    """把超过保留期的资讯与调用记录搬到归档表，再从热表删除。

    热表只留近期数据，页面查询与索引不会被历史数据拖慢；
    归档表保留全量副本，需要追溯时可按 archived_at 查询。
    """
    cutoff_day = (date.today() - timedelta(days=days)).isoformat()
    cutoff_ts = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    now = _now()

    with _connect() as conn:
        intel_pending = conn.execute(
            "SELECT COUNT(*) FROM daily_intel WHERE date < ?", (cutoff_day,)
        ).fetchone()[0]
        calls_pending = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE COALESCE(created_at, '') < ?", (cutoff_ts,)
        ).fetchone()[0]

        pending = {"intel": int(intel_pending), "llm_calls": int(calls_pending)}
        if dry_run or not (intel_pending or calls_pending):
            return {**pending, "cutoff_day": cutoff_day, "cutoff_ts": cutoff_ts,
                    "dry_run": dry_run}

        conn.execute(
            """
            INSERT INTO daily_intel_archive
                (date, keyword, title, summary, sentiment, source, url, fetched_at, archived_at)
            SELECT date, keyword, title, summary, sentiment, source, url, fetched_at, ?
              FROM daily_intel WHERE date < ?
            """,
            (now, cutoff_day),
        )
        intel_moved = conn.execute(
            "DELETE FROM daily_intel WHERE date < ?", (cutoff_day,)
        ).rowcount

        conn.execute(
            """
            INSERT INTO llm_calls_archive
                (run_id, node, model, prompt_tokens, completion_tokens, total_tokens,
                 latency_ms, ok, error, created_at, archived_at)
            SELECT run_id, node, model, prompt_tokens, completion_tokens, total_tokens,
                   latency_ms, ok, error, created_at, ?
              FROM llm_calls WHERE COALESCE(created_at, '') < ?
            """,
            (now, cutoff_ts),
        )
        calls_moved = conn.execute(
            "DELETE FROM llm_calls WHERE COALESCE(created_at, '') < ?", (cutoff_ts,)
        ).rowcount

    return {
        "intel": int(intel_moved or 0),
        "llm_calls": int(calls_moved or 0),
        "cutoff_day": cutoff_day,
        "cutoff_ts": cutoff_ts,
        "dry_run": False,
    }


def storage_stats() -> dict:
    """热表与归档表的行数，供运维查看。"""
    with _connect() as conn:
        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "daily_intel": count("daily_intel"),
            "daily_intel_archive": count("daily_intel_archive"),
            "llm_calls": count("llm_calls"),
            "llm_calls_archive": count("llm_calls_archive"),
            "analyses": count("analyses"),
            "run_logs": count("run_logs"),
            "alerts": count("alerts"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse 数据维护")
    parser.add_argument(
        "--archive-days", type=int, default=90,
        help="超过该天数的数据归档并从热表删除（默认 90 天）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不执行归档")
    parser.add_argument("--stats", action="store_true", help="查看各表行数")
    args = parser.parse_args()

    init_db()
    if args.stats:
        print(json.dumps(storage_stats(), ensure_ascii=False, indent=2))
        return 0

    result = archive_old(days=args.archive_days, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print(json.dumps(storage_stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
