"""数据源与关键词的唯一注册表。

fetcher / app / scheduler_service 都从这里取值，避免同一份配置散落多处。
"""

from __future__ import annotations

# 逻辑数据源 → [(实际展示源名, RSS 地址)]，按顺序尝试，失败自动回退
# 注：机器之心官方 RSS 已失效（返回 HTML），回退到量子位；Twitter/X 无公开 RSS
SOURCES = {
    "36Kr": [("36Kr", "https://www.36kr.com/feed")],
    "机器之心": [
        ("机器之心", "https://www.jiqizhixin.com/rss"),
        ("量子位", "https://www.qbitai.com/feed"),
    ],
    "HackerNews": [("HackerNews", "https://hnrss.org/newest?q={q}")],
    "TechCrunch": [("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/")],
    "arXiv": [("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI")],
    "InfoQ": [("InfoQ 中文", "https://www.infoq.cn/feed")],
    "Solidot": [("Solidot", "https://www.solidot.org/index.rss")],
}

# UI 与调度器的默认数据源顺序
DATA_SOURCES = list(SOURCES)

# 监控关键词（顺序即 UI 下拉框顺序）
KEYWORDS = ["大模型", "AI Agent", "具身智能", "RAG"]

# HackerNews 是关键词检索型 RSS，需要英文检索词
SEARCH_TERMS = {
    "大模型": "LLM",
    "AI Agent": "agent",
    "具身智能": "robot",
    "RAG": "RAG",
}

# 监控关键词 → 中英文匹配词
KEYWORD_ALIASES = {
    "大模型": [
        "大模型", "大语言模型", "llm", "large language model",
        "gpt", "claude", "gemini", "llama", "deepseek",
    ],
    "AI Agent": [
        "ai agent", "agents", "agentic", "agent", "智能体", "多智能体",
    ],
    "具身智能": [
        "具身智能", "embodied", "humanoid", "robot", "robotics", "机器人",
    ],
    "RAG": [
        "rag", "retrieval-augmented", "retrieval augmented", "检索增强", "向量检索",
    ],
}
