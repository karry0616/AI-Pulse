"""Critic 契约校验回归测试：覆盖 8 类打回与数值归一。"""

import pytest

from agents import critic
from agents.state import AgentState
from rag.memory import MemoryContext


def make_state(items=None):
    items = items if items is not None else [{"title": f"OpenAI 发布第{i}代模型"} for i in range(5)]
    return AgentState(
        run_id="test",
        keyword="AI Agent",
        items=items,
        memory=MemoryContext(comparison="相比上周同期，该话题的负面情绪基本持平"),
    )


def valid_draft():
    return {
        "heat_score": 1200,
        "positive_ratio": "65%",
        "negative_alerts": 2,
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
        "warning_text": "监测到两条负面信号，涉及服务稳定性与数据合规，建议人工复核后再决定是否推送。",
        "comparison": "持平",
    }


def test_accepts_valid_draft():
    assert critic.check_contract(valid_draft(), make_state()) == []


@pytest.mark.parametrize(
    "mutate, keyword",
    [
        (lambda d: d.pop("comparison"), "缺少字段"),
        (lambda d: d.update(heat_score=9999), "heat_score"),
        (lambda d: d.update(positive_ratio="65"), "positive_ratio"),
        (lambda d: d.update(negative_alerts=99), "negative_alerts"),
        (lambda d: d.update(top_news="- **A**：a"), "top_news"),
        (lambda d: d.update(maturity="不是表格"), "maturity"),
        (lambda d: d.update(warning_text="太短"), "warning_text"),
        (lambda d: d.update(comparison="   "), "comparison"),
    ],
)
def test_rejects_bad_draft(mutate, keyword):
    draft = valid_draft()
    mutate(draft)
    issues = critic.check_contract(draft, make_state())
    assert any(keyword in issue for issue in issues)


def test_rejects_bad_json_shapes():
    assert critic.check_contract(None, make_state()) == ["输出不是 JSON 对象"]
    assert critic.check_contract({"_raw": "not json"}, make_state()) == [
        "输出无法解析为合法 JSON"
    ]


def test_rejects_fabricated_top_news_titles():
    draft = valid_draft()
    draft["top_news"] = "- **某公司发布超级模型**：a\n- **另一家公司融资十亿**：b\n- **行业迎来重大突破**：c"
    issues = critic.check_contract(draft, make_state())
    assert any("可核验" in issue for issue in issues)


def test_accepts_top_news_matching_input_titles():
    draft = valid_draft()
    draft["top_news"] = "- **OpenAI 发布第0代模型**：a\n- **OpenAI 发布第3代模型**：b\n- **OpenAI 发布第4代模型**：c"
    assert critic.check_contract(draft, make_state()) == []


def test_finalize_clamps_out_of_range_values():
    state = make_state()
    state.draft = {**valid_draft(), "heat_score": 9999, "positive_ratio": "120%", "negative_alerts": 99}
    result = critic.finalize(state)
    assert result["heat_score"] == 2000
    assert result["positive_ratio"] == "100%"
    assert result["negative_alerts"] == 5


def test_finalize_fills_defaults_for_empty_draft():
    state = make_state()
    state.draft = {}
    result = critic.finalize(state)
    assert result["heat_score"] == 1000
    assert result["negative_alerts"] == 0
    assert result["comparison"] == state.memory.comparison
    assert result["_passed"] is False
