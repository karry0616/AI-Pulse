"""Plotly 图表构建：与页面布局解耦，便于复用与单测。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

ACCENT = "#7C5CFF"
MUTED = "#8B93A7"
PLOTLY_FONT = "Inter, 'Segoe UI', 'Microsoft YaHei', sans-serif"


def build_heat_chart(df: pd.DataFrame) -> go.Figure:
    """近 7 天讨论热度折线图（带渐变阴影填充）。"""
    last_x, last_y = df["日期"].iloc[-1], df["讨论热度"].iloc[-1]
    y_max = max(float(df["讨论热度"].max()), 1)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["日期"],
            y=df["讨论热度"],
            mode="lines+markers",
            name="讨论热度",
            line=dict(color=ACCENT, width=3, shape="spline", smoothing=0.6),
            marker=dict(size=7, color="#0B0F19", line=dict(color=ACCENT, width=2)),
            fill="tozeroy",
            fillcolor="rgba(124, 92, 255, .16)",
            hovertemplate="<b>%{x}</b><br>讨论热度 %{y:,}<extra></extra>",
        )
    )
    # 最新点为高亮光晕
    fig.add_trace(
        go.Scatter(
            x=[last_x],
            y=[last_y],
            mode="markers",
            marker=dict(size=16, color="rgba(124, 92, 255, .22)"),
            hoverinfo="skip",
            showlegend=False,
        )
    )
    fig.add_annotation(
        x=last_x,
        y=last_y,
        text=f"{last_y:,}",
        showarrow=False,
        yshift=18,
        font=dict(size=12, color="#C4B5FD"),
        bgcolor="rgba(124, 92, 255, .16)",
        borderpad=4,
    )
    fig.update_layout(
        height=300,
        margin=dict(l=8, r=14, t=24, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=PLOTLY_FONT, color=MUTED, size=12),
        showlegend=False,
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="#131A2A",
            bordercolor="rgba(124, 92, 255, .4)",
            font=dict(color="#E6E8F0", family=PLOTLY_FONT),
        ),
        xaxis=dict(
            showgrid=False,
            linecolor="rgba(255,255,255,.08)",
            tickfont=dict(color=MUTED, size=11),
            fixedrange=True,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,.06)",
            zeroline=False,
            range=[0, y_max * 1.25],
            tickfont=dict(color=MUTED, size=11),
            tickformat=",",
            fixedrange=True,
        ),
    )
    return fig


def build_sentiment_chart(df: pd.DataFrame) -> go.Figure:
    """近 3 天情绪分布堆叠柱状图。"""
    fig = go.Figure()
    series = [
        ("正面", "rgba(52, 211, 153, .85)"),
        ("中性", "rgba(100, 116, 139, .65)"),
        ("负面", "rgba(248, 113, 113, .85)"),
    ]
    for name, color in series:
        fig.add_trace(
            go.Bar(
                name=name,
                x=df["日期"],
                y=df[name],
                marker=dict(color=color, line=dict(width=0)),
                hovertemplate="<b>%{x}</b><br>" + name + " %{y} 条<extra></extra>",
            )
        )
    fig.update_layout(
        height=300,
        margin=dict(l=8, r=14, t=30, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=PLOTLY_FONT, color=MUTED, size=12),
        barmode="stack",
        bargap=0.38,
        hovermode="x",
        hoverlabel=dict(
            bgcolor="#131A2A",
            bordercolor="rgba(124, 92, 255, .4)",
            font=dict(color="#E6E8F0", family=PLOTLY_FONT),
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            bgcolor="rgba(0,0,0,0)",
            font=dict(color=MUTED, size=11),
        ),
        xaxis=dict(
            type="category",
            showgrid=False,
            linecolor="rgba(255,255,255,.08)",
            tickfont=dict(color=MUTED, size=11),
            fixedrange=True,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,.06)",
            zeroline=False,
            tickfont=dict(color=MUTED, size=11),
            fixedrange=True,
        ),
    )
    return fig
