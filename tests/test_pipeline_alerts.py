"""审批流测试：push_alert / reject_alert 的各状态分支与审计回写。"""

import pytest

import database
import notify
import pipeline


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database.DB_PATH


def test_push_alert_missing_returns_failure():
    result = pipeline.push_alert(9999, log=lambda _m: None)
    assert result.ok is False
    assert "不存在" in result.detail


def test_push_alert_sent_is_idempotent():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "sent", "HTTP 200")

    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is True
    assert result.detail == "已发送"


def test_push_alert_rejected_is_refused():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "rejected")

    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is False
    assert result.detail == "已驳回"


def test_push_alert_expired_cannot_be_sent():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "expired")

    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is False
    assert result.detail == "expired"


def test_push_alert_claim_lost_to_concurrent_approver(monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")

    def fake_claim(_alert_id, *_a, **_k):
        database.mark_alert(_alert_id, "sent", "HTTP 200")
        return False

    monkeypatch.setattr(database, "claim_alert", fake_claim)
    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is True
    assert result.detail == "已发送"


def test_push_alert_claim_lost_while_other_request_sending(monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    monkeypatch.setattr(database, "claim_alert", lambda *_a, **_k: False)

    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is False
    assert "正在被其他审批处理" in result.detail


def test_push_alert_success_marks_sent_with_reviewer(monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    monkeypatch.setattr(
        notify,
        "send_webhook",
        lambda **kwargs: notify.PushResult(True, "飞书", "HTTP 200"),
    )

    result = pipeline.push_alert(
        alert_id, log=lambda _m: None, reviewed_by="karry", review_note="确认属实"
    )

    assert result.ok is True
    alert = database.get_alert(alert_id)
    assert alert["status"] == "sent"
    assert alert["reviewed_by"] == "karry"
    assert alert["review_note"] == "确认属实"
    assert alert["sent_at"]


def test_push_alert_failure_marks_failed_and_can_retry(monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    monkeypatch.setattr(
        notify,
        "send_webhook",
        lambda **kwargs: notify.PushResult(False, "飞书", "HTTP 500"),
    )

    result = pipeline.push_alert(alert_id, log=lambda _m: None)

    assert result.ok is False
    alert = database.get_alert(alert_id)
    assert alert["status"] == "failed"
    # failed 状态允许再次审批推送
    assert database.claim_alert(alert_id) is True


def test_reject_alert_missing_returns_failure():
    outcome = pipeline.reject_alert(9999, log=lambda _m: None)
    assert outcome["ok"] is False
    assert outcome["reason"] == "预警不存在"


def test_reject_alert_refuses_while_sending():
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    database.mark_alert(alert_id, "sending")

    outcome = pipeline.reject_alert(alert_id, log=lambda _m: None)

    assert outcome["ok"] is False
    assert "推送中" in outcome["reason"]
