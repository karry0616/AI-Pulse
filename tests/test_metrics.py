"""运行指标测试：分位数语义 + 汇总口径。"""

import pytest

import database
import metrics


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "metrics.db")
    database.init_db()


def test_percentile_needs_three_samples():
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([120], 50) == 120
    assert metrics.percentile([100, 300], 95) == 300


def test_percentile_interpolates():
    values = list(range(1, 101))  # 1..100
    assert metrics.percentile(values, 50) == pytest.approx(50.5)
    assert metrics.percentile(values, 95) == pytest.approx(95.05)


def test_summary_is_empty_without_runs():
    summary = metrics.summary(days=30)
    assert summary["runs"] == 0
    assert summary["success_rate"] is None
    assert summary["p95_ms"] is None


def test_summary_computes_rates_from_run_logs():
    database.start_run("r1", "manual", "AI Agent", ["36Kr", "InfoQ"])
    database.finish_run(
        "r1", "success", items=4, inserted=2, mode="deepseek", duration_ms=5000,
        sources_ok=2, sources_total=2, rejections=1, degraded=0, fetch_ms=1500,
    )
    database.start_run("r2", "manual", "AI Agent", ["36Kr"])
    database.finish_run(
        "r2", "success", items=2, inserted=0, mode="fallback", duration_ms=3000,
        sources_ok=0, sources_total=1, rejections=0, degraded=1, fetch_ms=800,
    )
    database.start_run("r3", "manual", "AI Agent", ["36Kr"])
    database.finish_run("r3", "failed", duration_ms=900, error="boom")

    summary = metrics.summary(days=30)

    assert summary["runs"] == 3
    assert summary["success_rate"] == pytest.approx(66.7)
    assert summary["degraded_rate"] == pytest.approx(50.0)
    assert summary["fetch_ok_ratio"] == pytest.approx(66.7)
    assert summary["rejections_per_run"] == pytest.approx(0.5)
    assert summary["avg_ms"] == 4000
    assert summary["avg_fetch_ms"] == 1150


def test_series_only_returns_successful_runs():
    database.start_run("r1", "manual", "AI Agent", ["36Kr"])
    database.finish_run("r1", "success", mode="fallback", duration_ms=2000, degraded=1)
    database.start_run("r2", "manual", "AI Agent", ["36Kr"])
    database.finish_run("r2", "failed", duration_ms=500)

    rows = metrics.series(limit=5)

    assert len(rows) == 1
    assert rows[0]["duration_ms"] == 2000
    assert rows[0]["degraded"] == 1
