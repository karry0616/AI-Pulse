"""pipeline 纯函数测试：热度评分只由抓取结果决定。"""

import database
import fetcher
import pipeline
import reports
from llm import client
from rag import memory as rag_memory
from rag import store as rag_store


def test_compute_heat_grows_with_items_and_sources():
    few = pipeline.compute_heat([{"source": "TechCrunch"}])
    many = pipeline.compute_heat(
        [{"source": name} for name in ["TechCrunch", "36Kr", "arXiv", "InfoQ", "Solidot"] for _ in range(4)]
    )
    assert 800 <= few < many <= 2000


def test_compute_heat_is_bounded():
    assert pipeline.compute_heat([]) == 800
    huge = pipeline.compute_heat([{"source": f"s{i}"} for i in range(200)])
    assert huge == 2000


def test_compute_heat_ignores_empty_sources():
    assert pipeline.compute_heat([{"source": ""}]) == pipeline.compute_heat([{}])


def test_pad_analysis_items_fills_floor_from_archive():
    fresh = [{"title": "新增 A"}]
    archived = [{"title": "存档 B"}, {"title": "存档 C"}, {"title": "存档 D"}, {"title": "存档 E"}]

    merged = pipeline.pad_analysis_items(fresh, archived, floor=3)

    assert [item["title"] for item in merged] == ["新增 A", "存档 B", "存档 C"]


def test_pad_analysis_items_dedupes_and_keeps_order():
    fresh = [{"title": "同一标题"}]
    archived = [{"title": "同一标题"}, {"title": "另一条"}]

    merged = pipeline.pad_analysis_items(fresh, archived, floor=2)

    assert [item["title"] for item in merged] == ["同一标题", "另一条"]


def test_pad_analysis_items_is_noop_when_enough():
    fresh = [{"title": f"新增 {i}"} for i in range(5)]
    assert pipeline.pad_analysis_items(fresh, [{"title": "存档"}]) == fresh


def test_run_bootstraps_fresh_database(tmp_path, monkeypatch):
    """全新数据库上直接调用 pipeline.run() 不应崩溃（P0-2 回归）。

    入口自己负责建表，不能依赖 app.py / api.py 先调用 init_db()。
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / ".pulse.lock")
    monkeypatch.setattr(fetcher, "clear_cache", lambda: None)
    monkeypatch.setattr(fetcher, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(client, "available", lambda: False)
    monkeypatch.setattr(rag_store, "available", lambda: False)
    monkeypatch.setattr(rag_store, "index_items", lambda *a, **k: 0)
    monkeypatch.setattr(rag_store, "backfill", lambda *a, **k: 0)
    monkeypatch.setattr(
        rag_memory,
        "build_context",
        lambda *a, **k: rag_memory.MemoryContext(comparison="暂无历史对比"),
    )
    monkeypatch.setattr(reports, "save", lambda *a, **k: tmp_path / "report.md")

    result = pipeline.run("AI Agent", ["36Kr"], trigger="test", log=lambda _m: None)

    assert result.analysis["_mode"] == "fallback"
    rows = database.recent_runs(limit=1)
    assert rows[0]["status"] == "success"
    # 运行指标随 run 一起落库（v5 新增列）
    assert rows[0]["sources_total"] == 1
    assert rows[0]["degraded"] == 1

