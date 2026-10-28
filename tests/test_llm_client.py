"""DeepSeek 客户端测试：重试退避、失败计数、JSON 提取。"""

import pytest

from llm import client


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def ok_body(content="{}"):
    return {
        "model": "deepseek-chat",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)
    logged = []
    monkeypatch.setattr(client.database, "log_llm_call", lambda *a, **k: logged.append((a, k)))
    return logged


def test_retries_then_succeeds(monkeypatch):
    responses = [FakeResponse(503, {"error": {"message": "busy"}}), FakeResponse(200, ok_body())]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(client.requests, "post", fake_post)
    result = client.chat([{"role": "user", "content": "hi"}], node="Test", run_id="r", log=lambda m: None)
    assert len(calls) == 2
    assert result.total_tokens == 15


def test_gives_up_after_max_retries(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(500, {"error": {"message": "boom"}})

    monkeypatch.setattr(client.requests, "post", fake_post)
    with pytest.raises(client.LLMCallError):
        client.chat([{"role": "user", "content": "hi"}], node="Test", run_id="r", log=lambda m: None)
    assert len(calls) == client.MAX_RETRIES + 1


def test_extract_json_handles_code_fences():
    assert client.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert client.extract_json('{"a": 1}') == {"a": 1}
    assert client.extract_json("not json") is None
    assert client.extract_json("[1, 2]") is None
