"""AI Pulse · AI 行业情报监控中枢

数据链路：RSS 抓取 → SQLite → Chroma 历史记忆 → Collector/Analyst/Critic
→ 预警暂存 → 人工审核后 Webhook 推送。

运行：streamlit run app.py
"""

import os
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

import config
import database
import notify
import pipeline
from ui import charts, trace

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
STYLE_PATH = Path(__file__).resolve().parent / "ui" / "style.css"
st.markdown(
    f"<style>{STYLE_PATH.read_text(encoding='utf-8')}</style>",
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
        except pipeline.RunLocked as exc:
            add_log(f"[WARN] {exc}")
            status.update(label="已有任务正在运行", state="error", expanded=True)
            return "已有任务正在运行，请稍后再试", "⏳"
        except Exception as exc:  # 保证 UI 不会因为单个环节异常而白屏
            add_log(f"[WARN] 更新失败：{type(exc).__name__}: {exc}")
            status.update(label="情报更新失败", state="error", expanded=True)
            return "情报更新失败，请查看底部日志", "⚠️"

    mode = "DeepSeek" if result.analysis["_mode"] == "deepseek" else "本地规则"
    return f"已更新 {result.items} 条情报（新增 {result.inserted}）· {mode} 分析完成", "⚡"


# ---------------------------------------------------------------------------
# 4. 图表与执行轨迹（已拆分至 ui/charts.py · ui/trace.py）
# ---------------------------------------------------------------------------


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
                charts.build_heat_chart(heat_df),
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
                charts.build_sentiment_chart(sentiment_df),
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
elif alert_row and alert_row["status"] == "expired":
    alert_footer = "⌛ 超过 24 小时未审核，已自动过期"
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
        trace.render_trace(analysis)

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
