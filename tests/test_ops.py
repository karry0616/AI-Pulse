"""运维与安全加固测试：归档留存、分析回放、按钮冷却、审批审计、卡片按钮、密钥自检、日志落盘。"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import config
import database
import logging_setup
import notify
import pipeline


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database.DB_PATH


def make_item(title="新闻A", keyword="AI Agent", day: str | None = None):
    return {
        "date": day or date.today().isoformat(),
        "keyword": keyword,
        "title": title,
        "summary": "摘要",
        "sentiment": "neutral",
        "source": "test",
        "url": "https://example.com",
    }


# ---------------------------------------------------------------------------
# P2-17 归档留存
# ---------------------------------------------------------------------------
def test_archive_moves_old_rows_and_keeps_recent():
    old_day = (date.today() - timedelta(days=200)).isoformat()
    database.save_items([make_item("很老的新闻", day=old_day), make_item("今天的新闻")])

    result = database.archive_old(days=90)

    assert result["intel"] == 1
    stats = database.storage_stats()
    assert stats["daily_intel"] == 1
    assert stats["daily_intel_archive"] == 1
    assert database.fetch_recent(days=365)["title"].tolist() == ["今天的新闻"]


def test_archive_dry_run_does_not_delete():
    old_day = (date.today() - timedelta(days=200)).isoformat()
    database.save_items([make_item("很老的新闻", day=old_day)])

    result = database.archive_old(days=90, dry_run=True)

    assert result["intel"] == 1 and result["dry_run"] is True
    assert database.storage_stats()["daily_intel"] == 1


def test_archive_also_moves_old_llm_calls():
    database.log_llm_call("run-old", "Analyst", "deepseek-chat", 10, 5, 100)
    old_ts = (datetime.now() - timedelta(days=120)).isoformat(timespec="seconds")
    with database._connect() as conn:
        conn.execute("UPDATE llm_calls SET created_at = ?", (old_ts,))

    result = database.archive_old(days=90)

    assert result["llm_calls"] == 1
    stats = database.storage_stats()
    assert stats["llm_calls"] == 0
    assert stats["llm_calls_archive"] == 1


# ---------------------------------------------------------------------------
# P3-5 分析结果持久化
# ---------------------------------------------------------------------------
def test_analysis_roundtrip_and_latest_wins():
    database.save_analysis("run-1", "AI Agent", {"heat_score": 1234, "_mode": "deepseek"})
    assert database.load_analysis("AI Agent")["heat_score"] == 1234

    database.save_analysis("run-2", "AI Agent", {"heat_score": 999, "_mode": "fallback"})
    assert database.load_analysis("AI Agent")["heat_score"] == 999
    assert database.load_analysis("RAG") is None


def test_save_analysis_is_idempotent_per_run():
    database.save_analysis("run-1", "AI Agent", {"heat_score": 1000})
    database.save_analysis("run-1", "AI Agent", {"heat_score": 1500})
    assert database.storage_stats()["analyses"] == 1
    assert database.load_analysis("AI Agent")["heat_score"] == 1500


# ---------------------------------------------------------------------------
# P3-4 按钮冷却
# ---------------------------------------------------------------------------
def test_cooldown_counts_down_from_last_success():
    database.start_run("run-1", "manual", "AI Agent", ["36Kr"])
    database.finish_run("run-1", "success", items=1, inserted=1)

    remaining = pipeline.cooldown_remaining("AI Agent", window=90)
    assert 0 < remaining <= 90
    assert pipeline.cooldown_remaining("RAG", window=90) == 0


def test_cooldown_ignores_failed_runs():
    database.start_run("run-1", "manual", "AI Agent", ["36Kr"])
    database.finish_run("run-1", "failed", error="boom")
    assert pipeline.cooldown_remaining("AI Agent", window=90) == 0


def test_cooldown_can_be_disabled_by_env(monkeypatch):
    database.start_run("run-1", "manual", "AI Agent", ["36Kr"])
    database.finish_run("run-1", "success")
    monkeypatch.setattr(pipeline, "cooldown_window", lambda: 0)
    assert pipeline.cooldown_remaining("AI Agent") == 0


# ---------------------------------------------------------------------------
# P2-15 审批审计
# ---------------------------------------------------------------------------
def test_reject_alert_writes_audit_fields():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")

    outcome = pipeline.reject_alert(
        alert_id, log=lambda m: None, reviewed_by="karry", review_note="误报"
    )

    assert outcome["ok"] is True
    alert = database.get_alert(alert_id)
    assert alert["status"] == "rejected"
    assert alert["reviewed_by"] == "karry"
    assert alert["review_note"] == "误报"
    assert alert["reviewed_at"]


def test_reject_sent_alert_is_refused():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "sent", "HTTP 200", reviewed_by="karry")

    assert pipeline.reject_alert(alert_id, log=lambda m: None)["ok"] is False


def test_mark_alert_sets_sent_at_only_when_sent():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "failed", "HTTP 500")
    assert database.get_alert(alert_id)["sent_at"] is None

    database.mark_alert(alert_id, "sent", "HTTP 200")
    assert database.get_alert(alert_id)["sent_at"]


def test_rejected_alert_is_not_revived_by_dedup():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    pipeline.reject_alert(alert_id, log=lambda m: None, reviewed_by="karry")

    new_id = database.save_alert("run-2", "AI Agent", "新一轮预警")
    assert new_id != alert_id


# ---------------------------------------------------------------------------
# P2-16 飞书卡片按钮
# ---------------------------------------------------------------------------
def test_feishu_card_has_action_buttons(monkeypatch):
    monkeypatch.setattr(notify, "public_base_url", lambda: "https://pulse.example.com")
    card = notify._payload(
        "https://open.feishu.cn/open-apis/bot/v2/hook/xxx", "正文", alert_id=7
    )

    tags = [element["tag"] for element in card["card"]["elements"]]
    assert "action" in tags
    actions = card["card"]["elements"][tags.index("action")]["actions"]
    assert [item["value"]["action"] for item in actions] == ["approve", "reject"]
    assert actions[0]["value"]["alert_id"] == "7"


def test_feishu_card_without_base_url_has_no_buttons(monkeypatch):
    monkeypatch.setattr(notify, "public_base_url", lambda: "")
    card = notify._payload(
        "https://open.feishu.cn/open-apis/bot/v2/hook/xxx", "正文", alert_id=7
    )
    assert [element["tag"] for element in card["card"]["elements"]] == ["markdown"]


def test_dingtalk_payload_stays_markdown(monkeypatch):
    monkeypatch.setattr(notify, "public_base_url", lambda: "https://pulse.example.com")
    payload = notify._payload(
        "https://oapi.dingtalk.com/robot/send?access_token=x", "正文", alert_id=7
    )
    assert payload["msgtype"] == "markdown"
    assert "sign" not in payload


# ---------------------------------------------------------------------------
# P3-2 密钥治理
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(config, "load_env", lambda *a, **k: None)
    for key in config.REQUIRED_KEYS + config.OPTIONAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_secret_status_masks_value(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-1234567890abcdef")
    status = config.secret_status()
    assert status["DEEPSEEK_API_KEY"]["configured"] is True
    assert status["DEEPSEEK_API_KEY"]["masked"] == "sk-1…def"
    assert "1234567890" not in status["DEEPSEEK_API_KEY"]["masked"]


def test_missing_secrets_reports_required_only(clean_env):
    assert config.missing_secrets() == ["DEEPSEEK_API_KEY"]
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-x")
    assert config.missing_secrets() == []


def test_secret_warnings_cover_webhook_and_callback(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-x")
    warnings = config.secret_warnings()
    assert any("WEBHOOK_URL" in item for item in warnings)

    clean_env.setenv("WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    warnings = config.secret_warnings()
    assert any("PUBLIC_BASE_URL" in item for item in warnings)


# ---------------------------------------------------------------------------
# P3-3 日志落盘
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_logger(tmp_path, monkeypatch):
    import logging

    logger = logging.getLogger("pulse")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    monkeypatch.setattr(logging_setup, "LOG_FILE", tmp_path / "pulse.log")
    monkeypatch.setattr(logging_setup, "_configured", False)
    yield tmp_path / "pulse.log"

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    monkeypatch.setattr(logging_setup, "_configured", False)


def test_log_writes_to_file(fresh_logger: Path):
    logging_setup.log("[OK] 落盘验证")
    logging_setup.log("[WARN] 告警也要落盘")

    for handler in __import__("logging").getLogger("pulse").handlers:
        handler.flush()

    content = fresh_logger.read_text(encoding="utf-8")
    assert "落盘验证" in content
    assert "WARN" in content


def test_pipeline_tee_log_writes_both(fresh_logger: Path):
    captured: list[str] = []
    emit = pipeline._tee_log(captured.append)
    emit("[TEST] 双写")

    for handler in __import__("logging").getLogger("pulse").handlers:
        handler.flush()

    assert captured == ["[TEST] 双写"]
    assert "双写" in fresh_logger.read_text(encoding="utf-8")


def test_pipeline_tee_log_skips_already_file_backed_sink(fresh_logger: Path):
    """API 的 _log 自己落盘，tee 不应再写一次导致重复行。"""
    import api

    emit = pipeline._tee_log(api._log)
    assert emit is api._log

    emit("[TEST] 只落一次")
    for handler in __import__("logging").getLogger("pulse").handlers:
        handler.flush()

    assert fresh_logger.read_text(encoding="utf-8").count("只落一次") == 1
