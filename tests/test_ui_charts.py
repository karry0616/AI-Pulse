"""图表构建测试：只验证数据结构正确，不校验视觉细节。"""

import pandas as pd
import plotly.graph_objects as go

from ui import charts


def test_build_heat_chart_has_line_and_highlight():
    df = pd.DataFrame({"日期": ["10-28", "10-29"], "讨论热度": [120, 300]})
    fig = charts.build_heat_chart(df)
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 2  # 主线 + 最新点光晕
    assert list(fig.data[0].y) == [120, 300]


def test_build_sentiment_chart_stacks_three_series():
    df = pd.DataFrame(
        {
            "日期": ["10-29"],
            "正面": [3],
            "中性": [5],
            "负面": [2],
        }
    )
    fig = charts.build_sentiment_chart(df)
    assert [trace.name for trace in fig.data] == ["正面", "中性", "负面"]
    assert fig.layout.barmode == "stack"


def test_build_duration_chart_marks_degraded_runs():
    rows = [
        {"label": "10-29 10:00", "duration_ms": 3200, "degraded": 0},
        {"label": "10-29 11:00", "duration_ms": 2500, "degraded": 1},
    ]
    fig = charts.build_duration_chart(rows)
    assert list(fig.data[0].y) == [3.2, 2.5]
    colors = list(fig.data[0].marker.color)
    assert "124, 92, 255" in colors[0]
    assert "248, 113, 113" in colors[1]
