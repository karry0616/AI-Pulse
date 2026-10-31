"""执行层回归：Collector / Analyst / Critic 状态机与规则降级路径。

这些用例补的是「契约层之外的执行层」——降级、重试、跳过重试的判定
全部在这里，改动 graph.py 时能否回归就看这一组。
"""

import pytest

from agents import analyst, collector, critic, graph
from agents.state import AgentState
from llm import client
from rag.memory import MemoryContext

ITEMS = [
    {"title": f"OpenAI 发布第{i}代模型", "sentiment": "neutral", "source": "test"}
    for i in range(5)
]
NEGATIVE_ITEMS = [
    {"title": "某大厂裁员 30%，项目组整体解散", "sentiment": "negative", "source": "test"},
    {"title": "模型服务宕机 6 小时，客户投诉激增", "sentiment": "negative", "source": "test"},
]


def make_state(items=None) -> AgentState:
    return AgentState(
        run_id="test",
        keyword="AI Agent",
        items=items if items is not None else list(ITEMS),
        memory=MemoryContext(comparison="相比上周同期，该话题的负面情绪基本持平"),
    )


def quiet(_message: str) -> None:
    return None


def valid_draft() -> dict:
    return {
        "heat_score": 1200,
        "positive_ratio": "65%",
        "negative_alerts": 1,
        "top_news": (
            "- **OpenAI 发布第0代模型**：a\n"
            "- **OpenAI 发布第1代模型**：b\n"
            "- **OpenAI 发布第2代模型**：c"
        ),
        "maturity": (
            "| 方向 | 成熟度 | 阶段判断 |\n"
            "| :--- | :--- | :--- |\n"
            "| 大模型 | ●●●●○ | 生产可用 |\n"
            "| RAG | ●●●●○ | 工程化成熟 |"
        ),
        "warning_text": "监测到一条负面信号，涉及服务稳定性，建议人工复核后再决定是否推送。",
        "comparison": "持平",
    }


BAD_DRAFT = {
    "heat_score": "很高",
    "positive_ratio": "65",
    "negative_alerts": 9,
    "top_news": "只有一行",
    "maturity": "不是表格",
    "warning_text": "太短",
}


# ---------------------------------------------------------------------------
# 规则模式兜底文案：必须满足 Critic 的 30-200 字契约
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("items", [ITEMS, NEGATIVE_ITEMS, []])
def test_fallback_warning_meets_contract(items):
    draft = analyst.fallback(make_state(items), "无 Key", quiet)
    assert 30 <= len(draft["warning_text"]) <= 200


def test_fallback_short_text_is_padded():
    tail = "建议人工复核后再决定是否推送；规则模式仅做信号提示，不作为结论依据。"
    assert 30 <= len(analyst._ensure_warning("太短", tail)) <= 200


def test_fallback_long_text_is_truncated():
    assert len(analyst._ensure_warning("很" * 400, "")) == analyst.WARNING_MAX


def test_fallback_draft_passes_contract_in_rule_mode():
    """规则模式的完整输出应当通过契约，否则降级就永远不达标。"""
    state = make_state()
    draft = analyst.fallback(state, "无 Key", quiet)
    assert critic.check_contract(draft, state) == []


# ---------------------------------------------------------------------------
# 状态机：降级不重试、模型路径最多 3 次
# ---------------------------------------------------------------------------
def test_graph_skips_retry_when_rule_mode(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: False)
    logs: list[str] = []

    result = graph.run(list(ITEMS), "AI Agent", make_state().memory, run_id="t", log=logs.append)

    assert result["_mode"] == "fallback"
    assert result["_attempts"] == 1  # 只跑一次，不再空转 3 轮
    assert result["_rejections"] == 0
    assert result["_passed"] is True
    assert not any("打回第" in line for line in logs)


def test_graph_without_items_stops_immediately(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    result = graph.run([], "AI Agent", MemoryContext(comparison="无历史"), run_id="t", log=quiet)
    assert result["_attempts"] == 0
    assert result["_mode"] == "fallback"
    assert result["_events"] == 0


def test_graph_retries_three_times_then_degrades(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    calls: list[int] = []

    def always_bad(state, log=print):
        state.mode = "deepseek"
        calls.append(state.attempts)
        return dict(BAD_DRAFT)

    monkeypatch.setattr(analyst, "run", always_bad)
    logs: list[str] = []

    result = graph.run(list(ITEMS), "AI Agent", make_state().memory, run_id="t", log=logs.append)

    assert len(calls) == 3
    assert result["_attempts"] == 3
    assert result["_mode"] == "fallback"
    assert result["_passed"] is False
    assert result["_rejections"] == 3
    assert any("达到最大重试次数" in line for line in logs)


def test_graph_passes_on_second_attempt(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    drafts = [dict(BAD_DRAFT), valid_draft()]

    def staged(state, log=print):
        state.mode = "deepseek"
        return drafts[min(state.attempts, len(drafts)) - 1]

    monkeypatch.setattr(analyst, "run", staged)

    result = graph.run(list(ITEMS), "AI Agent", make_state().memory, run_id="t", log=quiet)

    assert result["_attempts"] == 2
    assert result["_passed"] is True
    assert result["_rejections"] == 1


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
def test_collector_fallback_events_use_item_sentiment(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: False)
    state = make_state(NEGATIVE_ITEMS)

    events = collector.run(state, quiet)

    assert len(events) == len(NEGATIVE_ITEMS)
    assert all(event["sentiment"] == "negative" for event in events)
    assert state.degraded is True


def test_collector_sanitize_drops_invalid_and_dedupes():
    raw = [
        {"title": "正常标题", "sentiment": "positive", "entities": ["A", "B", "C", "D", "E", "F"]},
        {"title": "正常标题", "sentiment": "negative"},
        {"title": "", "sentiment": "weird"},
        "not-a-dict",
        {"title": "另一条", "sentiment": "weird"},
    ]
    events = collector._sanitize(raw)

    assert [event["title"] for event in events] == ["正常标题", "另一条"]
    assert len(events[0]["entities"]) == 5  # 实体最多保留 5 个
    assert events[1]["sentiment"] == "neutral"  # 非法情感归一到 neutral


def test_collector_falls_back_when_llm_returns_empty(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    monkeypatch.setattr(
        client,
        "chat",
        lambda *a, **k: client.ChatResult("{}", "deepseek-chat", 10, 5, 20),
    )
    state = make_state()
    events = collector.run(state, quiet)
    assert len(events) == len(ITEMS)
    assert state.degraded is True


def test_collector_uses_llm_events_when_valid(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    payload = '{"events": [{"title": "OpenAI 发布第0代模型", "sentiment": "positive", "entities": ["OpenAI"]}]}'
    monkeypatch.setattr(
        client,
        "chat",
        lambda *a, **k: client.ChatResult(payload, "deepseek-chat", 10, 5, 20),
    )
    state = make_state()
    events = collector.run(state, quiet)
    assert events[0]["entities"] == ["OpenAI"]
    assert state.degraded is False


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------
def test_analyst_returns_raw_on_unparsable_output(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)
    monkeypatch.setattr(
        client,
        "chat",
        lambda *a, **k: client.ChatResult("这不是 JSON", "deepseek-chat", 10, 5, 20),
    )
    state = make_state()
    draft = analyst.run(state, quiet)
    assert "_raw" in draft
    assert critic.check_contract(draft, state) == ["输出无法解析为合法 JSON"]


def test_analyst_degrades_on_call_error(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: True)

    def boom(*args, **kwargs):
        raise client.LLMCallError("HTTP 500")

    monkeypatch.setattr(client, "chat", boom)
    state = make_state()
    draft = analyst.run(state, quiet)
    assert state.mode == "fallback"
    assert 30 <= len(draft["warning_text"]) <= 200
