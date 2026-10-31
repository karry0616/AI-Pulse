"""Webhook 推送测试：渠道识别、钉钉加签、重试与失败分支。"""

import requests

import notify


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_channel_name_detects_dingtalk_and_feishu():
    assert notify.channel_name("https://oapi.dingtalk.com/robot/send?x=1") == "钉钉"
    assert notify.channel_name("https://open.feishu.cn/open-apis/bot/v2/hook/x") == "飞书"
    assert notify.channel_name("https://open.larksuite.com/open-apis/bot/v2/hook/x") == "飞书"
    assert notify.channel_name("https://example.com/hook") == "Webhook"


def test_build_markdown_includes_heat_only_when_present():
    without = notify.build_markdown("正文", "AI Agent", None)
    with_heat = notify.build_markdown("正文", "AI Agent", 1500)
    assert "舆情热度" not in without
    assert "**舆情热度**：1500" in with_heat
    assert "AI Agent" in with_heat


def test_sign_dingtalk_appends_timestamp_and_sign(monkeypatch):
    monkeypatch.setattr(notify.time, "time", lambda: 1700000000.0)
    signed = notify.sign_dingtalk(
        "https://oapi.dingtalk.com/robot/send?access_token=t", "secret"
    )
    assert "timestamp=1700000000000" in signed
    assert "&sign=" in signed


def test_send_webhook_without_url_returns_failure(monkeypatch):
    monkeypatch.setattr(notify, "webhook_url", lambda: "")
    result = notify.send_webhook("预警", log=lambda _m: None)
    assert result.ok is False
    assert "未配置" in result.detail


def test_send_webhook_success(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: FakeResponse(200, {"code": 0}))
    logs: list[str] = []

    result = notify.send_webhook(
        "预警", url="https://open.feishu.cn/hook/x", log=logs.append
    )

    assert result.ok is True
    assert result.channel == "飞书"
    assert any("推送成功" in line for line in logs)


def test_send_webhook_non_zero_code_is_failure(monkeypatch):
    monkeypatch.setattr(
        notify.requests, "post", lambda *a, **k: FakeResponse(200, {"code": 19001})
    )
    monkeypatch.setattr(notify.time, "sleep", lambda _s: None)

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is False
    assert "19001" in result.detail


def test_send_webhook_retries_on_retryable_status(monkeypatch):
    responses = [FakeResponse(503, None), FakeResponse(200, {"StatusCode": 0})]
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setattr(notify.time, "sleep", lambda _s: None)

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is True
    assert len(calls) == 2


def test_send_webhook_gives_up_after_retry_budget(monkeypatch):
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse(500, None)

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setattr(notify.time, "sleep", lambda _s: None)

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is False
    assert len(calls) == notify.WEBHOOK_RETRIES + 1


def test_send_webhook_does_not_retry_on_client_error(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: FakeResponse(400, None))

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is False
    assert "HTTP 400" in result.detail


def test_send_webhook_retries_after_request_exception(monkeypatch):
    attempts: list[int] = []

    def fake_post(url, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ConnectionError("boom")
        return FakeResponse(200, {})

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setattr(notify.time, "sleep", lambda _s: None)

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is True
    assert len(attempts) == 2


def test_send_webhook_returns_last_error_when_all_attempts_raise(monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setattr(notify.time, "sleep", lambda _s: None)

    result = notify.send_webhook("预警", url="https://example.com/hook", log=lambda _m: None)

    assert result.ok is False
    assert "Timeout" in result.detail


def test_send_webhook_signs_dingtalk_target(monkeypatch):
    captured: list[str] = []

    def fake_post(url, **kwargs):
        captured.append(url)
        return FakeResponse(200, {"errcode": 0})

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setattr(notify, "dingtalk_secret", lambda: "s3cret")

    result = notify.send_webhook(
        "预警", url="https://oapi.dingtalk.com/robot/send?access_token=t", log=lambda _m: None
    )

    assert result.ok is True
    assert "sign=" in captured[0]
