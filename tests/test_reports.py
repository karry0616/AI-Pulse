"""日报导出测试：渲染内容完整、文件名按日期+关键词生成。"""

from datetime import date

import reports


def make_analysis(**overrides):
    data = {
        "heat_score": 1234,
        "positive_ratio": "62%",
        "negative_alerts": 2,
        "top_news": "- **新闻A**：解读A\n- **新闻B**：解读B\n- **新闻C**：解读C",
        "maturity": "| 方向 | 成熟度 | 阶段判断 |\n| :--- | :--- | :--- |\n| 大模型 | ●●●●○ | 生产可用 |",
        "warning_text": "监测到两条负面信号，建议人工复核后再决定是否推送。",
        "comparison": "负面情绪较上周下降",
        "_mode": "deepseek",
        "_attempts": 1,
        "_duration_ms": 4200,
    }
    data.update(overrides)
    return data


def test_render_contains_all_sections():
    text = reports.render(
        make_analysis(),
        "AI Agent",
        "run-1",
        items=8,
        inserted=5,
        usage={"calls": 3, "total_tokens": 900, "prompt_tokens": 700, "completion_tokens": 200, "latency_ms": 3000},
        alert_id=12,
        level="HIGH",
    )
    for section in ("核心动态", "成熟度", "风险预警", "历史对比", "成本"):
        assert section in text
    assert "预警 #12" in text
    assert "900" in text


def test_save_writes_dated_markdown(tmp_path):
    path = reports.save(make_analysis(), "AI Agent", "run-1", directory=tmp_path)
    assert path.name == f"{date.today().isoformat()}-AI Agent.md"
    assert path.read_text(encoding="utf-8").startswith("# AI Pulse 日报")


def test_save_sanitizes_keyword_in_filename(tmp_path):
    path = reports.save(make_analysis(), "AI/Agent", "run-1", directory=tmp_path)
    assert "/" not in path.name
