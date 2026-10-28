"""SQLite 持久层测试：全部走临时库，不触碰真实 pulse.db。"""

from datetime import date, datetime, timedelta

import pytest

import database


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


def make_item(title="新闻A", keyword="AI Agent", sentiment="neutral"):
    return {
        "date": date.today().isoformat(),
        "keyword": keyword,
        "title": title,
        "summary": "摘要",
        "sentiment": sentiment,
        "source": "test",
        "url": "https://example.com",
    }


def test_save_items_deduplicates_by_date_and_title():
    items = [make_item("新闻A"), make_item("新闻B")]
    assert database.save_items(items) == 2
    assert database.save_items(items) == 0


def test_fetch_recent_filters_by_keyword():
    database.save_items([make_item("新闻A", "AI Agent"), make_item("新闻B", "大模型")])
    df = database.fetch_recent(days=1, keyword="大模型")
    assert df["title"].tolist() == ["新闻B"]


def test_daily_stats_counts_sentiment():
    database.save_items(
        [
            make_item("正面新闻", sentiment="positive"),
            make_item("负面新闻", sentiment="negative"),
        ]
    )
    stats = database.daily_stats(days=1)
    assert int(stats.iloc[0]["total"]) == 2
    assert int(stats.iloc[0]["positive"]) == 1
    assert int(stats.iloc[0]["negative"]) == 1


def test_run_log_lifecycle():
    database.start_run("run-1", "manual", "AI Agent", ["TechCrunch"])
    database.finish_run("run-1", "success", items=3, inserted=2, mode="deepseek", duration_ms=1200)
    run = database.recent_runs(limit=1)[0]
    assert run["status"] == "success"
    assert run["inserted"] == 2


def test_llm_usage_summary():
    database.log_llm_call("run-1", "Analyst", "deepseek-chat", 100, 50, 800)
    database.log_llm_call("run-1", "Collector", "deepseek-chat", 200, 30, 500)
    usage = database.usage_summary("run-1")
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 380


def test_alert_deduplicates_within_same_day():
    first = database.save_alert("run-1", "AI Agent", "第一次预警文案", level="LOW", heat_score=1000)
    second = database.save_alert("run-2", "AI Agent", "第二次预警文案", level="HIGH", heat_score=1500)
    assert first == second
    alert = database.get_alert(first)
    assert alert["warning_text"] == "第二次预警文案"
    assert alert["level"] == "HIGH"
    assert alert["run_id"] == "run-2"
    assert alert["status"] == "pending"


def test_alert_deduplicates_after_failed_push():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "failed", "HTTP 500")
    again = database.save_alert("run-2", "AI Agent", "新预警文案")
    assert again == alert_id
    assert database.get_alert(alert_id)["status"] == "pending"


def test_expire_stale_alerts_only_touches_old_pending():
    fresh = database.save_alert("run-1", "AI Agent", "今天的预警")
    old = database.save_alert("run-0", "大模型", "两天前的预警")
    old_time = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    with database._connect() as conn:
        conn.execute("UPDATE alerts SET created_at = ? WHERE id = ?", (old_time, old))

    assert database.expire_stale_alerts(hours=24) == 1
    assert database.get_alert(old)["status"] == "expired"
    assert database.get_alert(fresh)["status"] == "pending"
