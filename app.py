"""AI Pulse · AI 行业情报监控中枢

数据链路：RSS 抓取 → SQLite → Chroma 历史记忆 → Collector/Analyst/Critic
→ 预警暂存 → 人工审核后 Webhook 推送。

运行：streamlit run app.py
"""

import os
from datetime import date, datetime, timedelta
from html import escape

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import database
import notify
import pipeline

config.load_env()

# ---------------------------------------------------------------------------
# 1. 页面配置
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="AI Pulse · AI 行业情报监控中枢",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#7C5CFF"
CYAN = "#22D3EE"
GREEN = "#34D399"
AMBER = "#FBBF24"
RED = "#F87171"
MUTED = "#8B93A7"
PLOTLY_FONT = "Inter, 'Segoe UI', 'Microsoft YaHei', sans-serif"

# ---------------------------------------------------------------------------
# 2. 数据链路与会话状态
# ---------------------------------------------------------------------------
KEYWORDS = ["大模型", "AI Agent", "具身智能", "RAG"]
DATA_SOURCES = ["TechCrunch", "机器之心", "36Kr", "HackerNews", "Twitter/X"]

database.init_db()

if "logs" not in st.session_state:
    st.session_state.logs = [
        (datetime.now().strftime("%H:%M:%S"), "Agent 待命，点击「立即更新情报」启动抓取", "info")
    ]
if "analysis" not in st.session_state:
    st.session_state.analysis = None
if "analysis_keyword" not in st.session_state:
    st.session_state.analysis_keyword = None
if "webhook_ok" not in st.session_state:
    st.session_state.webhook_ok = None
if "last_run" not in st.session_state:
    st.session_state.last_run = None
if "last_run_id" not in st.session_state:
    st.session_state.last_run_id = None
if "pending_alert_id" not in st.session_state:
    st.session_state.pending_alert_id = None

# ---------------------------------------------------------------------------
# 3. 全局样式（暗色 + 紧凑排版）
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
:root {
  --accent: #7C5CFF;
  --text: #E6E8F0;
  --muted: #8B93A7;
  --line: rgba(255, 255, 255, .07);
}

html, body { background-color: #070B14; }
.stApp {
  background:
    radial-gradient(1100px 520px at 10% -10%, rgba(124, 92, 255, .18), transparent 60%),
    radial-gradient(900px 480px at 96% 0%, rgba(34, 211, 238, .08), transparent 55%),
    #070B14;
  color: var(--text);
  font-family: 'Inter', 'Segoe UI', 'Microsoft YaHei', sans-serif;
}
[data-testid="stAppViewContainer"] { background: transparent; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"], #MainMenu, footer { display: none; }
.block-container { padding: 1.5rem 2.2rem 2.2rem; max-width: 1500px; }

/* ---------- 文本 ---------- */
h1, h2, h3, h4 { color: #F5F7FF; }
.stMarkdown p, .stMarkdown li { color: #C9D1E0; }
[data-testid="stWidgetLabel"] p {
  color: #8B93A7 !important;
  font-size: .78rem !important;
  font-weight: 600;
  letter-spacing: .02em;
}

/* ---------- 顶部标题 ---------- */
.hero-title {
  font-size: 1.9rem;
  font-weight: 800;
  letter-spacing: -.02em;
  line-height: 1.3;
  color: #F5F7FF;
}
.hero-title .grad {
  background: linear-gradient(100deg, #A78BFA 0%, #22D3EE 90%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.hero-sub {
  margin-top: 4px;
  font-size: .68rem;
  letter-spacing: .18em;
  color: #5F6A82;
  text-transform: uppercase;
}
.hr-fade {
  height: 1px;
  margin: .95rem 0 .25rem;
  background: linear-gradient(90deg, rgba(124, 92, 255, .45), rgba(34, 211, 238, .14), transparent);
}

/* ---------- 分区标题 ---------- */
.sec-head {
  display: flex;
  align-items: baseline;
  gap: 9px;
  margin: 1.25rem 0 .65rem;
}
.sec-bar {
  display: inline-block;
  width: 3px;
  height: 15px;
  border-radius: 2px;
  background: linear-gradient(180deg, #7C5CFF, #22D3EE);
}
.sec-title { font-size: .95rem; font-weight: 700; color: #F5F7FF; }
.sec-cap { font-size: .7rem; color: #5F6A82; }

/* ---------- 卡片容器 ---------- */
div[data-testid="stVerticalBlockBorderWrapper"] {
  background: linear-gradient(165deg, rgba(23, 31, 50, .82), rgba(11, 16, 28, .9));
  border: 1px solid var(--line) !important;
  border-radius: 16px !important;
  box-shadow: 0 18px 40px rgba(0, 0, 0, .25);
}

/* ---------- KPI ---------- */
[data-testid="stMetricValue"] {
  font-size: 2.05rem !important;
  font-weight: 800 !important;
  letter-spacing: -.02em;
  color: #F5F7FF;
}
[data-testid="stMetricLabel"] p {
  font-size: .78rem !important;
  font-weight: 600;
  color: #93A0B8 !important;
  letter-spacing: .03em;
}
[data-testid="stMetricDelta"] { font-size: .78rem !important; font-weight: 600; }
.st-key-kpi_alert [data-testid="stMetricValue"] { color: #F87171 !important; }
.st-key-kpi_alert [data-testid="stMetricDelta"] { color: #F87171 !important; }
[data-testid="stCaptionContainer"] p { color: #5F6A82 !important; font-size: .72rem; }

/* ---------- 图表卡标题 ---------- */
.chart-title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin: .1rem 0 -.1rem;
  font-size: .86rem;
  font-weight: 700;
  color: #E6E8F0;
}
.chart-title span { font-size: .68rem; font-weight: 500; color: #5F6A82; }

/* ---------- 分析报告 ---------- */
.stMarkdown h3 {
  font-size: 1rem;
  font-weight: 700;
  margin: 1rem 0 .45rem;
  color: #F5F7FF;
}
.stMarkdown li { font-size: .88rem; line-height: 1.8; margin-bottom: 2px; }
.stMarkdown table {
  width: 100%;
  border-collapse: collapse;
  font-size: .82rem;
  margin: .5rem 0 .4rem;
}
.stMarkdown thead tr th {
  background: rgba(124, 92, 255, .12) !important;
  color: #C4B5FD !important;
  font-weight: 600;
  border-bottom: 1px solid rgba(124, 92, 255, .28) !important;
}
.stMarkdown tbody tr td {
  color: #C9D1E0 !important;
  border-bottom: 1px solid rgba(255, 255, 255, .05) !important;
}
.stMarkdown blockquote {
  margin: .75rem 0 0;
  padding: .7rem 1rem;
  border-left: 3px solid #7C5CFF;
  background: rgba(124, 92, 255, .07);
  border-radius: 0 10px 10px 0;
}
.stMarkdown blockquote p { font-size: .86rem; color: #C9C6E6; margin: 0; }

/* ---------- 预警卡 ---------- */
.alert-card {
  background: linear-gradient(165deg, rgba(66, 24, 34, .6), rgba(22, 13, 20, .92));
  border: 1px solid rgba(248, 113, 113, .26);
  border-radius: 16px;
  padding: 16px 16px 18px;
  box-shadow: 0 18px 40px rgba(0, 0, 0, .3);
}
.alert-top { display: flex; justify-content: space-between; align-items: center; }
.alert-title { font-size: .95rem; font-weight: 800; color: #FCA5A5; }
.alert-level {
  font-size: .6rem;
  font-weight: 800;
  letter-spacing: .1em;
  color: #FEE2E2;
  background: rgba(248, 113, 113, .2);
  border: 1px solid rgba(248, 113, 113, .4);
  padding: 2px 7px;
  border-radius: 6px;
}
.alert-kw {
  display: inline-block;
  margin-top: 10px;
  padding: 2px 8px;
  font-size: .68rem;
  color: #F59E0B;
  background: rgba(245, 158, 11, .08);
  border: 1px solid rgba(245, 158, 11, .22);
  border-radius: 6px;
}
.alert-body {
  margin: 10px 0 12px;
  font-size: .82rem;
  line-height: 1.75;
  color: #D8C8CE;
}
.alert-meta {
  padding-top: 10px;
  font-size: .7rem;
  line-height: 1.7;
  color: #8A7A82;
  border-top: 1px dashed rgba(248, 113, 113, .2);
}
.alert-footer {
  display: inline-block;
  margin-top: 12px;
  padding: 6px 10px;
  font-size: .72rem;
  font-weight: 600;
  color: #34D399;
  background: rgba(52, 211, 153, .1);
  border: 1px solid rgba(52, 211, 153, .28);
  border-radius: 8px;
}

/* ---------- 侧边栏 ---------- */
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #0B1120 0%, #080C16 100%);
  border-right: 1px solid rgba(255, 255, 255, .06);
}
[data-testid="stSidebar"] .block-container { padding-top: 1.3rem; }
.side-brand { font-size: 1.15rem; font-weight: 800; color: #F5F7FF; }
.side-sub {
  margin-bottom: 1.1rem;
  font-size: .62rem;
  letter-spacing: .18em;
  color: #5B6478;
  text-transform: uppercase;
}
.side-label {
  margin: 1.05rem 0 .55rem;
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .14em;
  color: #6F7A92;
  text-transform: uppercase;
}
.hist-card {
  margin-bottom: 8px;
  padding: 9px 12px 10px;
  background: rgba(19, 26, 42, .66);
  border: 1px solid rgba(255, 255, 255, .06);
  border-radius: 12px;
  transition: all .18s ease;
}
.hist-card:hover {
  background: rgba(28, 36, 58, .8);
  border-color: rgba(124, 92, 255, .45);
}
.hist-top { display: flex; justify-content: space-between; align-items: center; }
.hist-date { font-size: .68rem; color: #6F7A92; font-variant-numeric: tabular-nums; }
.hist-score { padding: 2px 8px; border-radius: 999px; font-size: .68rem; font-weight: 700; }
.score-good { color: #34D399; background: rgba(52, 211, 153, .12); border: 1px solid rgba(52, 211, 153, .25); }
.score-mid { color: #FBBF24; background: rgba(251, 191, 36, .1); border: 1px solid rgba(251, 191, 36, .22); }
.score-bad { color: #F87171; background: rgba(248, 113, 113, .1); border: 1px solid rgba(248, 113, 113, .22); }
.hist-kw { margin: 3px 0 7px; font-size: .85rem; font-weight: 600; color: #E6E8F0; }
.hist-bar { height: 4px; background: rgba(255, 255, 255, .07); border-radius: 999px; overflow: hidden; }
.hist-bar > span {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #7C5CFF, #22D3EE);
}

/* ---------- 控件 ---------- */
[data-baseweb="select"] > div {
  background-color: #101728 !important;
  border-color: rgba(255, 255, 255, .09) !important;
  border-radius: 10px !important;
}
[data-baseweb="select"] * { color: #E6E8F0 !important; }
[data-baseweb="tag"] {
  background: rgba(124, 92, 255, .16) !important;
  border: 1px solid rgba(124, 92, 255, .32) !important;
  border-radius: 7px !important;
}
[data-baseweb="tag"] span { color: #C4B5FD !important; }
[data-baseweb="popover"] ul { background: #101728 !important; }
[data-baseweb="popover"] li { color: #C9D1E0 !important; }
[data-baseweb="popover"] li:hover { background: rgba(124, 92, 255, .14) !important; }

.stButton > button[kind="primary"], [data-testid="stBaseButton-primary"] {
  padding: .5rem 1rem;
  border: none !important;
  border-radius: 10px !important;
  background: linear-gradient(135deg, #7C5CFF, #4F8CFF) !important;
  color: #FFFFFF !important;
  font-weight: 700;
  white-space: nowrap;
  box-shadow: 0 8px 22px rgba(124, 92, 255, .28);
  transition: all .18s ease;
}
.stButton > button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
  transform: translateY(-1px);
  box-shadow: 0 12px 28px rgba(124, 92, 255, .4);
}

/* ---------- 终端面板 ---------- */
.terminal {
  background: #05080F;
  border: 1px solid rgba(124, 92, 255, .22);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 22px 48px rgba(0, 0, 0, .4);
  font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace;
}
.term-bar {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 14px;
  background: rgba(255, 255, 255, .03);
  border-bottom: 1px solid rgba(255, 255, 255, .06);
}
.term-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.term-name { margin-left: 8px; font-size: .68rem; color: #5F6A82; letter-spacing: .04em; }
.term-status { margin-left: auto; font-size: .62rem; color: #34D399; letter-spacing: .1em; }
.term-body { padding: 14px 18px 16px; font-size: .78rem; line-height: 2.05; color: #C9D1E0; }
.term-time { color: #5B6478; }
.term-ok { color: #34D399; }
.term-warn { color: #FBBF24; }
.term-info { color: #22D3EE; }
.term-cursor { color: #34D399; animation: blink 1.1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* ---------- Agent 执行轨迹 ---------- */
[data-testid="stExpander"] details {
  background: rgba(15, 21, 36, .55);
  border: 1px solid rgba(255, 255, 255, .06) !important;
  border-radius: 14px !important;
}
[data-testid="stExpander"] summary {
  font-size: .82rem;
  font-weight: 700;
  color: #C9D1E0;
}
.trace-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: .3rem 0 .8rem;
}
.trace-card {
  padding: 11px 13px 12px;
  background: rgba(19, 26, 42, .7);
  border: 1px solid rgba(255, 255, 255, .07);
  border-radius: 12px;
}
.trace-node {
  font-size: .62rem;
  font-weight: 700;
  letter-spacing: .12em;
  color: #6F7A92;
  text-transform: uppercase;
}
.trace-val {
  margin-top: 5px;
  font-size: 1rem;
  font-weight: 800;
  color: #F5F7FF;
  font-variant-numeric: tabular-nums;
}
.trace-sub { margin-top: 3px; font-size: .68rem; color: #5F6A82; line-height: 1.6; }
.trace-ok { color: #34D399; }
.trace-warn { color: #FBBF24; }
.trace-bad { color: #F87171; }
.trace-label {
  margin: .9rem 0 .35rem;
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .12em;
  color: #6F7A92;
  text-transform: uppercase;
}
.trace-item { font-size: .78rem; line-height: 1.9; color: #C9D1E0; }
.trace-item .trace-dim { color: #5F6A82; }
.trace-issue { font-size: .76rem; line-height: 1.8; color: #FCA5A5; }

/* ---------- 页脚 ---------- */
.foot { margin-top: 1.4rem; text-align: center; font-size: .66rem; color: #46506A; letter-spacing: .08em; }

/* ---------- 滚动条 ---------- */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(124, 92, 255, .25); border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 数据管线：抓取 -> 入库 -> LLM 分析 -> Webhook
# ---------------------------------------------------------------------------
def _log_kind(message: str) -> str:
    if message.startswith("[WARN]"):
        return "warn"
    if message.startswith("[OK]"):
        return "ok"
    return "info"


def add_log(message: str) -> None:
    """把真实进度写进会话日志，供底部终端面板渲染。"""
    st.session_state.logs.append(
        (datetime.now().strftime("%H:%M:%S"), message, _log_kind(message))
    )
    st.session_state.logs = st.session_state.logs[-20:]


def run_pipeline(keyword: str, sources: list[str]) -> tuple[str, str]:
    """执行一次完整更新，返回 (toast 文案, 图标)。"""
    st.session_state.logs = []
    st.session_state.analysis = None
    st.session_state.webhook_ok = None
    st.session_state.pending_alert_id = None

    with st.status(f"Agent 正在更新「{keyword}」情报…", expanded=True) as status:
        try:
            result = pipeline.run(keyword, sources, trigger="manual", log=add_log)
            st.session_state.analysis = result.analysis
            st.session_state.analysis_keyword = keyword
            st.session_state.last_run_id = result.run_id
            st.session_state.pending_alert_id = result.alert_id
            st.session_state.last_run = datetime.now().strftime("%H:%M:%S")

            status.update(
                label=f"情报更新完成 · 新增 {result.inserted} 条",
                state="complete",
                expanded=False,
            )
        except Exception as exc:  # 保证 UI 不会因为单个环节异常而白屏
            add_log(f"[WARN] 更新失败：{type(exc).__name__}: {exc}")
            status.update(label="情报更新失败", state="error", expanded=True)
            return "情报更新失败，请查看底部日志", "⚠️"

    mode = "DeepSeek" if result.analysis["_mode"] == "deepseek" else "本地规则"
    return f"已更新 {result.items} 条情报（新增 {result.inserted}）· {mode} 分析完成", "⚡"


# ---------------------------------------------------------------------------
# 4. 绘图函数
# ---------------------------------------------------------------------------
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


def render_trace(analysis: dict) -> None:
    """把一次 run 的真实执行细节渲染成可演示的轨迹面板。"""
    run_id = analysis.get("_run_id") or ""
    calls = database.llm_calls_for_run(run_id) if run_id else []
    by_node: dict[str, list[dict]] = {}
    for call in calls:
        by_node.setdefault(call["node"], []).append(call)

    def node_card(label: str, node: str, extra: str, fallback_value: str) -> str:
        rows = by_node.get(node, [])
        if rows:
            tokens = sum(row["total_tokens"] for row in rows)
            latency = sum(row["latency_ms"] for row in rows)
            value = f"{tokens:,} tok"
            sub = f"{latency / 1000:.1f}s · {len(rows)} 次调用"
            cls = "trace-ok"
        else:
            value = fallback_value
            sub = "规则降级，无模型调用"
            cls = "trace-warn"
        return (
            f'<div class="trace-card">'
            f'<div class="trace-node">{label}</div>'
            f'<div class="trace-val {cls}">{value}</div>'
            f'<div class="trace-sub">{sub}{extra}</div>'
            f"</div>"
        )

    attempts = int(analysis.get("_attempts") or 0)
    passed = analysis.get("_passed")
    if passed is None:
        passed = analysis.get("_mode") == "deepseek"
    critic_value = f"第 {attempts} 次通过" if passed else "规则降级收尾"
    critic_cls = "trace-ok" if passed else "trace-warn"
    rag = analysis.get("_rag") or {}
    similar = rag.get("similar") or []
    rag_mode = "向量召回" if rag.get("mode") == "vector" else "关键词降级"

    duration = analysis.get("_duration_ms") or 0
    header = (
        f"run_id {run_id or '-'} · {analysis.get('_mode', '-')} · "
        f"事件 {analysis.get('_events', 0)} 条 · 全链路 {duration / 1000:.1f}s"
    )

    st.markdown(
        f'<div class="trace-item trace-dim" style="margin-bottom:.5rem">{escape(header)}</div>'
        '<div class="trace-grid">'
        + node_card("1 · Collector", "Collector", "", "规则提取")
        + node_card("2 · Analyst", "Analyst", f" · 尝试 {attempts} 次", "规则分析")
        + (
            f'<div class="trace-card">'
            f'<div class="trace-node">3 · Critic</div>'
            f'<div class="trace-val {critic_cls}">{critic_value}</div>'
            f'<div class="trace-sub">JSON 契约校验 · 规则判定</div>'
            f"</div>"
        )
        + (
            f'<div class="trace-card">'
            f'<div class="trace-node">RAG · 历史记忆</div>'
            f'<div class="trace-val trace-ok">{len(similar)} 条</div>'
            f'<div class="trace-sub">近 7 天召回 · {rag_mode}</div>'
            f"</div>"
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    issues = analysis.get("_issues") or []
    if issues:
        st.markdown('<div class="trace-label">Critic 打回记录</div>', unsafe_allow_html=True)
        st.markdown(
            "".join(f'<div class="trace-issue">· {escape(issue)}</div>' for issue in issues[:6]),
            unsafe_allow_html=True,
        )

    st.markdown('<div class="trace-label">RAG 历史召回</div>', unsafe_allow_html=True)
    if similar:
        rows = "".join(
            f'<div class="trace-item"><span class="trace-dim">[{item.get("date", "-")}]</span> '
            f'{escape(str(item.get("title", ""))[:70])} '
            f'<span class="trace-dim">· {escape(str(item.get("source", "")))} · '
            f'相似度 {item.get("similarity", 0)}</span></div>'
            for item in similar[:6]
        )
        st.markdown(rows, unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="trace-item trace-dim">本次未召回历史资讯（历史库为空或日期窗口外）</div>',
            unsafe_allow_html=True,
        )

    if rag.get("comparison"):
        st.markdown(
            f'<div class="trace-label">历史对比（周同比）</div>'
            f'<div class="trace-item">{escape(str(rag["comparison"]))}</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# 5. 侧边栏：数据源配置 + 历史记录
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        '<div class="side-brand">⚡ AI Pulse</div>'
        '<div class="side-sub">Intelligence Ops Console</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="side-label">数据源配置</div>', unsafe_allow_html=True)
    selected_sources = st.multiselect(
        "接入数据源",
        DATA_SOURCES,
        default=DATA_SOURCES,
        label_visibility="collapsed",
    )

    st.markdown('<div class="side-label">历史记录</div>', unsafe_allow_html=True)
    history = database.history_cards(limit=5)
    if history:
        for item in history:
            score = item["score"]
            score_cls = "score-good" if score >= 70 else "score-mid" if score >= 50 else "score-bad"
            st.markdown(
                f"""
                <div class="hist-card">
                  <div class="hist-top">
                    <span class="hist-date">{item["date"]}</span>
                    <span class="hist-score {score_cls}">{score} 分</span>
                  </div>
                  <div class="hist-kw">{item["keyword"]}</div>
                  <div class="hist-bar"><span style="width:{score}%"></span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.caption(f"共 {len(database.fetch_recent(days=30))} 条存档 · SQLite 实时数据")
    else:
        st.caption("暂无历史记录，点击「立即更新情报」开始采集")

    if not os.getenv("DEEPSEEK_API_KEY"):
        st.caption("⚠️ 未配置 DEEPSEEK_API_KEY，将走本地规则引擎")
    if not notify.webhook_configured():
        st.caption("⚠️ 未配置 WEBHOOK_URL，预警审批后无法推送")

# ---------------------------------------------------------------------------
# 6. 顶部 Header
# ---------------------------------------------------------------------------
head_left, head_btn, head_sel = st.columns([3.4, 1, 1.35], vertical_alignment="center")

with head_left:
    st.markdown(
        '<div class="hero-title">⚡ <span class="grad">AI Pulse</span> - AI 行业情报监控中枢</div>'
        '<div class="hero-sub">Realtime Signal · Sentiment · Risk Alert</div>',
        unsafe_allow_html=True,
    )

with head_btn:
    clicked = st.button("立即更新情报", type="primary", width="stretch")

with head_sel:
    keyword = st.selectbox("监控关键词", KEYWORDS, index=1, label_visibility="collapsed")

progress_slot = st.empty()
st.markdown('<div class="hr-fade"></div>', unsafe_allow_html=True)

if clicked:
    with progress_slot:
        toast_text, toast_icon = run_pipeline(keyword, selected_sources)
    progress_slot.empty()
    st.toast(toast_text, icon=toast_icon)

# 当前关键词对应的数据分析结果（切换关键词时自动失效）
analysis = st.session_state.analysis if st.session_state.analysis_keyword == keyword else None
snapshot = database.latest_snapshot(keyword)
trend = database.daily_stats(days=2, keyword=keyword)
today_total = snapshot["total"]
prev_total = int(trend["total"].iloc[0]) if len(trend) >= 2 else 0

# ---------------------------------------------------------------------------
# 7. 上部：核心 KPI 看板
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="sec-head"><span class="sec-bar"></span>'
    '<span class="sec-title">核心 KPI 看板</span>'
    f'<span class="sec-cap">{date.today().isoformat()} · {st.session_state.last_run or "--:--"} 快照 · SQLite 实时数据</span></div>',
    unsafe_allow_html=True,
)

k1, k2, k3 = st.columns(3, gap="medium")

heat_value = f"{analysis['heat_score']:,}" if analysis else f"{today_total:,}"
heat_delta = f"{(today_total - prev_total) / prev_total * 100:.0f}%" if prev_total else None
heat_caption = "全网提及量 · 较昨日" if prev_total else "全网提及量 · 暂无昨日基线"

if analysis:
    positive_value = analysis["positive_ratio"]
    sentiment_caption = f"LLM 情感判定 · 样本 {today_total:,} 条"
else:
    positive_value = f"{round(snapshot['positive'] / today_total * 100) if today_total else 0}%"
    sentiment_caption = f"规则引擎判定 · 样本 {today_total:,} 条"

alert_count = analysis["negative_alerts"] if analysis else snapshot["negative"]
alert_delta = "需人工复核" if alert_count else None

with k1:
    with st.container(border=True, key="kpi_heat"):
        st.metric("今日舆情热度", heat_value, heat_delta)
        st.caption(heat_caption)

with k2:
    with st.container(border=True, key="kpi_sentiment"):
        st.metric("正面情绪占比", positive_value)
        st.caption(sentiment_caption)

with k3:
    with st.container(border=True, key="kpi_alert"):
        st.metric("负面预警", f"{alert_count} 条", alert_delta, delta_color="inverse")
        st.caption("敏感词命中 · 需人工复核")

# ---------------------------------------------------------------------------
# 8. 中部：可视化图表
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="sec-head"><span class="sec-bar"></span>'
    '<span class="sec-title">趋势与情绪可视化</span>'
    f'<span class="sec-cap">{keyword} · 近 7 天 / 近 3 天</span></div>',
    unsafe_allow_html=True,
)

stats7 = database.daily_stats(days=7, keyword=keyword)

heat_df = (
    stats7[["date", "total"]]
    .rename(columns={"date": "日期", "total": "讨论热度"})
    .copy()
)
sentiment_df = (
    stats7.tail(3)[["date", "positive", "neutral", "negative"]]
    .rename(columns={"date": "日期", "positive": "正面", "neutral": "中性", "negative": "负面"})
    .copy()
)
for _df in (heat_df, sentiment_df):
    if not _df.empty:
        _df["日期"] = pd.to_datetime(_df["日期"]).dt.strftime("%m-%d")

chart_left, chart_right = st.columns(2, gap="medium")

with chart_left:
    with st.container(border=True):
        st.markdown(
            f'<div class="chart-title">讨论热度趋势 <span>{keyword} · 近 7 天</span></div>',
            unsafe_allow_html=True,
        )
        if heat_df.empty:
            st.info("暂无数据，点击「立即更新情报」开始采集")
        else:
            st.plotly_chart(
                build_heat_chart(heat_df),
                width="stretch",
                config={"displayModeBar": False},
            )

with chart_right:
    with st.container(border=True):
        st.markdown(
            '<div class="chart-title">情绪分布 <span>正面 / 中性 / 负面 · 近 3 天</span></div>',
            unsafe_allow_html=True,
        )
        if sentiment_df.empty:
            st.info("暂无数据，点击「立即更新情报」开始采集")
        else:
            st.plotly_chart(
                build_sentiment_chart(sentiment_df),
                width="stretch",
                config={"displayModeBar": False},
            )

# ---------------------------------------------------------------------------
# 9. 下部：情报分析与风险预警（7 : 3）
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="sec-head"><span class="sec-bar"></span>'
    '<span class="sec-title">情报分析与预警</span>'
    f'<span class="sec-cap">{"LLM 结构化摘要" if analysis else "等待 Agent 生成"} · 自动生成</span></div>',
    unsafe_allow_html=True,
)

if analysis:
    mode_text = "DeepSeek 多 Agent 协作" if analysis["_mode"] == "deepseek" else "本地规则引擎（降级）"
    comparison_md = (
        f"\n\n> **历史对比**：{analysis['comparison']}"
        if analysis.get("comparison") else ""
    )
    report_md = (
        f"### 🔥 今日核心动态\n\n{analysis['top_news']}\n\n"
        f"### 📊 技术成熟度判断\n\n{analysis['maturity']}{comparison_md}\n\n"
        f"> **生成方式**：{mode_text} · Critic 审核 {analysis.get('_attempts', 1)} 次 · "
        f"关键词「{keyword}」"
    )
else:
    report_md = (
        "### 🔥 今日核心动态\n\n暂未生成情报。点击右上角「立即更新情报」，"
        "Agent 会抓取 RSS → 写入历史记忆 → 多 Agent 输出结构化判断。\n\n"
        "### 📊 技术成熟度判断\n\n完成一次更新后，这里会显示模型给出的成熟度表格。"
    )

alert_row = None
if analysis:
    alert_run_id = analysis.get("_run_id") or st.session_state.last_run_id
    if alert_run_id:
        alert_row = database.latest_alert(alert_run_id)

if analysis:
    alert_level = "HIGH" if alert_count >= 2 else "MEDIUM" if alert_count == 1 else "LOW"
    alert_body = escape(analysis["warning_text"]).replace("\n", "<br />")
else:
    alert_level = "LOW"
    alert_body = "尚未生成预警内容。完成一次情报更新后，这里会展示模型识别出的负面信号摘要。"

if alert_row:
    alert_level = alert_row.get("level") or alert_level
    alert_body = escape(alert_row["warning_text"]).replace("\n", "<br />")

if alert_row and alert_row["status"] == "sent":
    alert_footer = f"✅ 已推送至 {notify.channel_name()}"
elif alert_row and alert_row["status"] == "failed":
    alert_footer = "⚠️ 推送失败，可重新审核发送"
elif alert_row and alert_row["status"] == "pending":
    alert_footer = "⏳ 预警待人工审核"
elif analysis and alert_count == 0:
    alert_footer = "⏸ 今日无预警，未触发推送"
elif analysis:
    alert_footer = "⚠️ 预警任务状态异常"
else:
    alert_footer = "⏳ 待生成情报"

alert_html = f"""
<div class="alert-card">
  <div class="alert-top">
    <div class="alert-title">⚠️ 风险预警</div>
    <span class="alert-level">{alert_level}</span>
  </div>
  <div class="alert-kw">命中规则 · 敏感词 / 负面情绪</div>
  <p class="alert-body">{alert_body}</p>
  <div class="alert-meta">
    来源：{'、'.join(selected_sources) or '未选择'}<br />
    今日入库：{today_total} 条 ｜ 负面信号：{alert_count} 条
  </div>
  <div class="alert-footer">{alert_footer}</div>
</div>
"""

report_col, alert_col = st.columns([7, 3], gap="medium")

with report_col:
    with st.container(border=True):
        st.markdown(report_md)

with alert_col:
    st.markdown(alert_html, unsafe_allow_html=True)
    if alert_row and alert_row["status"] in ("pending", "failed"):
        if st.button(
            "审核并发送",
            key=f"approve_alert_{alert_row['id']}",
            type="primary",
            width="stretch",
        ):
            with st.spinner("正在推送预警…"):
                push_result = pipeline.push_alert(alert_row["id"], log=add_log)
            st.session_state.webhook_ok = push_result.ok
            st.rerun()
    elif analysis and not alert_row and alert_count == 0:
        if st.button(
            "演示 HITL · 生成待审批预警",
            key="demo_alert_btn",
            width="stretch",
        ):
            demo_id = database.save_alert(
                run_id=analysis.get("_run_id") or st.session_state.last_run_id or "demo",
                keyword=keyword,
                warning_text=(
                    "[DEMO] 某头部大模型服务被曝连续 3 小时推理超时，开发者社区出现集中投诉，"
                    "官方尚未给出根因说明。本条为演示样例，非当日真实资讯。"
                ),
                level="MEDIUM",
                heat_score=analysis.get("heat_score"),
            )
            st.session_state.pending_alert_id = demo_id
            st.rerun()
        st.caption("插入一条标注 [DEMO] 的样例预警，用于演示人工审核与推送流程")

if analysis:
    with st.expander("Agent 执行轨迹 · Collector → Analyst → Critic", expanded=False):
        render_trace(analysis)

# ---------------------------------------------------------------------------
# 10. 底部：数据流转面板（终端黑框）
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="sec-head"><span class="sec-bar"></span>'
    '<span class="sec-title">数据流转日志</span>'
    '<span class="sec-cap">pulse-agent · pipeline.log</span></div>',
    unsafe_allow_html=True,
)

log_rows = "".join(
    f'<div><span class="term-time">[{ts}]</span> '
    f'<span class="term-{kind}">{escape(msg)}</span></div>'
    for ts, msg, kind in st.session_state.logs[-12:]
)
term_status = (
    f"● ONLINE · {st.session_state.last_run}" if st.session_state.last_run else "● IDLE"
)
term_cursor_time = st.session_state.last_run or "--:--:--"

st.markdown(
    f"""
    <div class="terminal">
      <div class="term-bar">
        <span class="term-dot" style="background:#F87171"></span>
        <span class="term-dot" style="background:#FBBF24"></span>
        <span class="term-dot" style="background:#34D399"></span>
        <span class="term-name">pulse-agent — pipeline.log</span>
        <span class="term-status">{term_status}</span>
      </div>
      <div class="term-body">
        {log_rows}
        <div><span class="term-time">[{term_cursor_time}]</span> <span class="term-cursor">▌</span></div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="foot">AI PULSE · AGENTIC INTELLIGENCE — '
    'RSS + Chroma RAG + Collector/Analyst/Critic + Human-in-the-loop</div>',
    unsafe_allow_html=True,
)
