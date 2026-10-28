"""HTTP API 测试：起真实本地服务，用 http 请求验证各端点。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import api
import database
import pipeline


class FakeResult:
    def __init__(self, run_id="run-fake", keyword="AI Agent"):
        self.run_id = run_id
        self.keyword = keyword
        self.analysis = {"_mode": "fallback", "heat_score": 1200}
        self.alert_id = None
        self.items = 3
        self.inserted = 2
        self.duration_ms = 1500
        self.usage = {}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    monkeypatch.setattr(pipeline, "cooldown_window", lambda: 90)
    return database.DB_PATH


@pytest.fixture
def base_url():
    server = api.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 只读端点
# ---------------------------------------------------------------------------
def test_health(base_url):
    status, body = call(base_url, "/api/health")
    assert status == 200 and body["ok"] is True


def test_keywords_lists_registry(base_url):
    status, body = call(base_url, "/api/keywords")
    assert status == 200
    assert "AI Agent" in body["keywords"]
    assert body["sources"]


def test_status_reports_cooldown_window(base_url):
    status, body = call(base_url, "/api/status")
    assert status == 200
    assert body["cooldown_seconds"] == 90
    assert body["lock"] is None


def test_alerts_filter_by_status(base_url):
    database.save_alert("run-1", "AI Agent", "待审预警")
    status, body = call(base_url, "/api/alerts?status=pending")
    assert status == 200
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["warning_text"] == "待审预警"


def test_intel_endpoint_returns_items(base_url):
    database.save_items([
        {
            "date": "2026-10-28",
            "keyword": "AI Agent",
            "title": "某条资讯",
            "summary": "",
            "sentiment": "neutral",
            "source": "test",
            "url": "",
        }
    ])
    status, body = call(base_url, "/api/intel?days=7")
    assert status == 200
    assert body["total"] >= 1


def test_threads_cost_storage_endpoints(base_url):
    assert call(base_url, "/api/threads")[0] == 200
    assert call(base_url, "/api/cost")[0] == 200
    storage = call(base_url, "/api/storage")[1]
    assert "daily_intel" in storage["storage"]


def test_unknown_path_returns_404(base_url):
    status, body = call(base_url, "/api/nope")
    assert status == 404 and body["ok"] is False


# ---------------------------------------------------------------------------
# 触发 run
# ---------------------------------------------------------------------------
def test_run_rejects_unknown_keyword(base_url):
    status, body = call(base_url, "/api/run", "POST", {"keyword": "不存在的词"})
    assert status == 400 and body["ok"] is False


def test_run_rejects_empty_sources(base_url):
    status, body = call(base_url, "/api/run", "POST", {"sources": ["不存在的源"]})
    assert status == 400


def test_run_wait_returns_analysis(base_url, monkeypatch):
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: FakeResult())

    status, body = call(
        base_url, "/api/run", "POST", {"keyword": "AI Agent", "wait": True}
    )

    assert status == 200
    assert body["run_id"] == "run-fake"
    assert body["inserted"] == 2
    assert body["mode"] == "fallback"


def test_run_is_blocked_by_cooldown(base_url):
    database.start_run("run-1", "manual", "AI Agent", ["36Kr"])
    database.finish_run("run-1", "success")

    status, body = call(base_url, "/api/run", "POST", {"keyword": "AI Agent"})

    assert status == 429
    assert body["retry_after"] > 0


def test_run_returns_409_when_locked(base_url, monkeypatch):
    monkeypatch.setattr(pipeline, "cooldown_remaining", lambda *a, **k: 0)

    def locked(*args, **kwargs):
        raise pipeline.RunLockedError("已有任务正在运行")

    monkeypatch.setattr(pipeline, "run", locked)

    status, body = call(
        base_url, "/api/run", "POST", {"keyword": "AI Agent", "wait": True}
    )
    assert status == 409


def test_run_async_returns_202(base_url, monkeypatch):
    triggered: list[str] = []

    def fake_run(keyword, sources, trigger="manual", log=print):
        triggered.append(keyword)
        return FakeResult(keyword=keyword)

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(pipeline, "cooldown_remaining", lambda *a, **k: 0)

    status, body = call(base_url, "/api/run", "POST", {"keyword": "AI Agent"})
    assert status == 202 and body["triggered"] is True

    for _ in range(50):
        if triggered:
            break
        threading.Event().wait(0.02)
    assert triggered == ["AI Agent"]


# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------
def test_approve_pushes_alert(base_url, monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    monkeypatch.setattr(
        pipeline,
        "push_alert",
        lambda alert_id, log=print, reviewed_by="", review_note="": type(
            "R", (), {"ok": True, "channel": "飞书", "detail": "HTTP 200"}
        )(),
    )

    status, body = call(
        base_url, "/api/approve", "POST",
        {"alert_id": alert_id, "action": "approve", "reviewed_by": "karry"},
    )

    assert status == 200 and body["pushed"] is True


def test_approve_missing_alert_returns_404(base_url):
    status, _ = call(base_url, "/api/approve", "POST", {"alert_id": 9999})
    assert status == 404


def test_approve_requires_alert_id(base_url):
    status, body = call(base_url, "/api/approve", "POST", {})
    assert status == 400 and "alert_id" in body["error"]


def test_reject_marks_alert_rejected(base_url):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")

    status, body = call(
        base_url, "/api/approve", "POST",
        {"alert_id": alert_id, "action": "reject", "reviewed_by": "karry", "review_note": "误报"},
    )

    assert status == 200 and body["ok"] is True
    alert = database.get_alert(alert_id)
    assert alert["status"] == "rejected"
    assert alert["reviewed_by"] == "karry"
    assert alert["review_note"] == "误报"


# ---------------------------------------------------------------------------
# 飞书卡片回调
# ---------------------------------------------------------------------------
def test_feishu_url_verification_echoes_challenge(base_url):
    status, body = call(
        base_url, "/api/callback/feishu", "POST",
        {"type": "url_verification", "challenge": "abc123"},
    )
    assert status == 200 and body["challenge"] == "abc123"


def test_feishu_card_approve_pushes(base_url, monkeypatch):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")
    seen: list[tuple] = []

    def fake_push(alert_id, log=print, reviewed_by="", review_note=""):
        seen.append((alert_id, reviewed_by, review_note))
        return type("R", (), {"ok": True, "channel": "飞书", "detail": "HTTP 200"})()

    monkeypatch.setattr(pipeline, "push_alert", fake_push)

    status, body = call(
        base_url, "/api/callback/feishu", "POST",
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "ou_123"},
                "action": {"value": {"action": "approve", "alert_id": str(alert_id)}},
            },
        },
    )

    assert status == 200 and body["ok"] is True
    assert seen == [(alert_id, "ou_123", "飞书卡片审批")]


def test_feishu_card_reject_marks_status(base_url):
    alert_id = database.save_alert("run-1", "AI Agent", "预警文案")

    status, body = call(
        base_url, "/api/callback/feishu", "POST",
        {"action": {"value": {"action": "reject", "alert_id": str(alert_id)}}},
    )

    assert status == 200 and body["ok"] is True
    assert database.get_alert(alert_id)["status"] == "rejected"


def test_feishu_callback_without_alert_id_returns_400(base_url):
    status, body = call(base_url, "/api/callback/feishu", "POST", {"action": {"value": {}}})
    assert status == 400
