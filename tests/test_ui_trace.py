"""Agent 执行轨迹面板测试：用假 st.markdown 捕获渲染出的 HTML。"""

import pytest

import database
from ui import trace


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database.DB_PATH


@pytest.fixture
def rendered(monkeypatch):
    """收集 render_trace 产生的所有 markdown 片段。"""
    chunks: list[str] = []

    def fake_markdown(body, **kwargs):
        chunks.append(body)

    monkeypatch.setattr(trace.st, "markdown", fake_markdown)
    return chunks


def test_render_trace_without_calls_shows_rule_fallback(rendered):
    trace.render_trace(
        {"_run_id": "run-empty", "_mode": "fallback", "_events": 0, "_duration_ms": 1200}
    )

    html = "".join(rendered)
    assert "规则降级，无模型调用" in html
    assert "规则降级收尾" in html
    assert "run-empty" in html
    assert "关键词降级" in html


def test_render_trace_with_calls_shows_tokens_and_latency(rendered):
    database.log_llm_call("run-1", "Analyst", "deepseek-chat", 120, 80, 1500)
    database.log_llm_call("run-1", "Analyst", "deepseek-chat", 100, 40, 900)

    trace.render_trace(
        {
            "_run_id": "run-1",
            "_mode": "deepseek",
            "_attempts": 2,
            "_passed": True,
            "_rag": {"similar": [], "comparison": "", "mode": "vector"},
        }
    )

    html = "".join(rendered)
    assert "340 tok" in html
    assert "2.4s · 2 次调用" in html
    assert "第 2 次通过" in html
    assert "向量召回" in html


def test_render_trace_shows_critic_issues_and_rag_items(rendered):
    trace.render_trace(
        {
            "_run_id": "",
            "_mode": "deepseek",
            "_attempts": 3,
            "_passed": False,
            "_issues": ["warning_text 过短", "heat_score 越界"],
            "_rag": {
                "similar": [
                    {"date": "2026-10-29", "title": "历史资讯", "source": "36Kr", "similarity": 0.82}
                ],
                "comparison": "正面占比同比 +8%",
                "mode": "jaccard",
            },
        }
    )

    html = "".join(rendered)
    assert "Critic 打回记录" in html
    assert "warning_text 过短" in html
    assert "历史资讯" in html
    assert "历史对比（周同比）" in html
    assert "正面占比同比 +8%" in html


def test_render_trace_without_rag_items_explains_empty_window(rendered):
    trace.render_trace({"_mode": "fallback", "_rag": {"similar": [], "mode": "jaccard"}})

    html = "".join(rendered)
    assert "本次未召回历史资讯" in html


def test_render_threads_with_data(rendered):
    trace._render_threads(
        {
            "linked": 4,
            "new": 1,
            "threads": 2,
            "window_days": 7,
            "top": [
                {
                    "title": "某大模型推理事故",
                    "entity": "DeepSeek",
                    "item_count": 3,
                    "last_date": "2026-10-30",
                }
            ],
        }
    )

    html = "".join(rendered)
    assert "事件线追踪" in html
    assert "DeepSeek" in html
    assert "3 篇报道" in html
    assert "本次归并 4 条资讯到 2 条事件线" in html
    assert "活跃窗口 7 天" in html


def test_render_threads_without_data(rendered):
    trace._render_threads({})

    html = "".join(rendered)
    assert "本次未形成事件线" in html
