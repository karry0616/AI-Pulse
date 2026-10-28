"""Agent 执行轨迹面板：把一次 run 的真实调用细节渲染成卡片。"""

from __future__ import annotations

from html import escape

import streamlit as st

import database


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
