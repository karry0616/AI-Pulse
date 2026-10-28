"""抓取层纯函数测试：清洗、关键词匹配、情感、日期解析。"""

import time
from datetime import date

import fetcher


def test_clean_text_strips_html_and_entities():
    raw = "<p>Hello&nbsp;<b>World</b></p>  "
    assert fetcher.clean_text(raw) == "Hello World"


def test_clean_text_applies_limit():
    assert len(fetcher.clean_text("a" * 200, 10)) == 10


def test_match_keyword_uses_aliases():
    assert fetcher.match_keyword("OpenAI 发布 agentic workflow", "AI Agent")
    assert fetcher.match_keyword("人形机器人开始量产", "具身智能")
    assert not fetcher.match_keyword("今天天气不错", "RAG")


def test_guess_sentiment_by_word_list():
    assert fetcher.guess_sentiment("OpenAI raises funding after breakthrough") == "positive"
    assert fetcher.guess_sentiment("Company faces lawsuit after data breach") == "negative"
    assert fetcher.guess_sentiment("A normal product update") == "neutral"


def test_guess_sentiment_uses_word_boundary():
    # risk-free 里的 risk 不应被算作负面词
    assert fetcher.guess_sentiment("discussing the risk-free rate") == "neutral"
    # 复数形式仍应命中
    assert fetcher.guess_sentiment("startup announces layoffs") == "negative"


def test_entry_date_falls_back_to_today():
    assert fetcher.entry_date({}) == date.today().isoformat()


def test_entry_date_reads_published_parsed():
    struct_time = time.gmtime(1704067200)  # 2024-01-01 00:00 UTC
    assert fetcher.entry_date({"published_parsed": struct_time}) == "2024-01-01"


def test_fetch_all_skips_source_without_rss():
    assert fetcher.fetch_all(["Twitter/X"], "AI Agent") == []


def test_keywords_have_search_terms():
    for keyword in fetcher.KEYWORD_ALIASES:
        assert keyword in fetcher.SEARCH_TERMS
