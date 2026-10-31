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


def test_fetch_source_uses_cache(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: calls.append("hit"))
    fetcher.clear_cache()
    fetcher._CACHE[("https://x.test/rss", "AI Agent", 5)] = (time.time(), [{"title": "cached"}])

    items = fetcher.fetch_source("X", "https://x.test/rss", "AI Agent", 5, lambda m: None)

    assert items == [{"title": "cached"}]
    assert calls == []  # 命中缓存不再发请求


def test_fetch_all_runs_sources_concurrently(monkeypatch):
    def slow_fetch(name, url, keyword, per_source, log):
        time.sleep(0.1)
        return []

    monkeypatch.setattr(fetcher, "fetch_source", slow_fetch)
    fetcher.clear_cache()
    names = list(fetcher.SOURCES)[:6]

    started = time.perf_counter()
    fetcher.fetch_all(names, "AI Agent", log=lambda m: None)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.4  # 串行至少 0.6s，并发应接近单次耗时


def test_fetch_all_preserves_source_order(monkeypatch):
    def fake_fetch(name, url, keyword, per_source, log):
        return [{"title": f"{name} 标题", "source": name}]

    monkeypatch.setattr(fetcher, "fetch_source", fake_fetch)
    fetcher.clear_cache()
    names = list(fetcher.SOURCES)[:3]

    items = fetcher.fetch_all(names, "AI Agent", log=lambda m: None)

    assert [item["source"] for item in items] == names


def test_failed_source_is_skipped_within_cooldown(monkeypatch):
    calls: list[str] = []

    def fail_fetch(name, url, keyword, per_source, log):
        calls.append(name)
        return None

    monkeypatch.setattr(fetcher, "fetch_source", fail_fetch)
    fetcher.clear_cache()

    assert fetcher.fetch_all(["36Kr"], "AI Agent", log=lambda m: None) == []
    assert fetcher.fetch_all(["36Kr"], "AI Agent", log=lambda m: None) == []
    assert len(calls) == 1  # 第二轮走失败冷却，不再发请求


def test_fetch_source_treats_empty_feed_as_success(monkeypatch):
    """合法 feed 当期无条目（如 arXiv 周末 skipDays）应算成功，而不是坏源。"""
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0"><channel><title>arXiv</title>'
        b"<skipDays><day>Saturday</day><day>Sunday</day></skipDays>"
        b"</channel></rss>"
    )

    class Response:
        content = body

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetcher.requests, "get", lambda *args, **kwargs: Response())
    fetcher.clear_cache()

    items = fetcher.fetch_source(
        "arXiv", "https://x.test/empty", "AI Agent", 5, lambda message: None
    )

    assert items == []


def test_fetch_source_rejects_non_feed_body(monkeypatch):
    class Response:
        content = b"<html><body>502 Bad Gateway</body></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetcher.requests, "get", lambda *args, **kwargs: Response())
    fetcher.clear_cache()

    items = fetcher.fetch_source("X", "https://x.test/broken", "AI Agent", 5, lambda m: None)

    assert items is None
