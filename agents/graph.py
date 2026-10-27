"""轻量状态机：Collector → Analyst → Critic →（打回重试 | 结束）。

节点式编排与 LangGraph 的 State/Node/Conditional Edge 一一对应，
但只依赖标准库，避免为三次串行调用引入额外的框架耦合。
"""

from __future__ import annotations

import argparse
import json
import sys

from rag.memory import MemoryContext, build_context

from . import analyst, collector, critic
from .state import AgentState

MAX_ATTEMPTS = 3


def run(
    items: list[dict],
    keyword: str,
    memory: MemoryContext,
    run_id: str = "-",
    log=print,
) -> dict:
    state = AgentState(
        run_id=run_id,
        keyword=keyword,
        items=items,
        memory=memory,
    )

    if not state.titles:
        state.draft = analyst.fallback(state, "今日没有抓到资讯", log)
        log("[AGENT] 无资讯，跳过 Collector / Analyst / Critic")
        return critic.finalize(state)

    collector.run(state, log)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        state.attempts = attempt
        state.draft = analyst.run(state, log)
        issues = critic.check_contract(state.draft, state)

        if not issues:
            state.passed = True
            state.critique = []
            state.critique_history.append([])
            log(f"[AGENT] Critic 审核通过（第 {attempt} 次）")
            break

        state.critique = issues
        state.critique_history.append(issues)
        log(f"[REFLEXION] Critic 打回第 {attempt} 次：{'；'.join(issues[:3])}")

    else:
        log("[REFLEXION] 达到最大重试次数，切换规则降级")
        state.draft = analyst.fallback(state, "Critic 连续 3 次未通过", log)
        state.passed = False

    analysis = critic.finalize(state)
    log(
        f"[AGENT] 流程结束：mode={analysis['_mode']} · "
        f"attempts={analysis['_attempts']} · 事件 {len(state.events)} 条"
    )
    return analysis


def main() -> int:
    parser = argparse.ArgumentParser(description="多 Agent 状态机自测")
    parser.add_argument("--keyword", default="AI Agent")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument(
        "--force-bad",
        action="store_true",
        help="用一份非法草稿验证 Critic 的契约检查",
    )
    args = parser.parse_args()

    import config
    import database

    config.load_env()
    database.init_db()

    def log(message: str) -> None:
        print(message)

    df = database.fetch_recent(days=args.days, keyword=args.keyword)
    items = df.to_dict("records") if not df.empty else []
    log(f"[AGENT] 读取到 {len(items)} 条资讯（最近 {args.days} 天）")

    memory = build_context(args.keyword, [item["title"] for item in items], log)

    if args.force_bad:
        state = AgentState(
            run_id="force_bad",
            keyword=args.keyword,
            items=items,
            memory=memory,
        )
        bad_draft = {
            "heat_score": "很高",
            "positive_ratio": "65",
            "negative_alerts": 9,
            "top_news": "只有一行",
            "maturity": "不是表格",
            "warning_text": "太短",
        }
        issues = critic.check_contract(bad_draft, state)
        log(f"[CRITIC] 检出 {len(issues)} 个问题：")
        for issue in issues:
            log(f"  - {issue}")
        return 0 if issues else 1

    analysis = run(items, args.keyword, memory, run_id="cli", log=log)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
