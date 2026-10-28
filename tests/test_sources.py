"""数据源注册表测试：关键词/检索词/别名/数据源保持一致。"""

import sources


def test_every_keyword_has_search_term_and_aliases():
    for keyword in sources.KEYWORDS:
        assert keyword in sources.SEARCH_TERMS
        assert sources.KEYWORD_ALIASES[keyword], f"{keyword} 缺少匹配别名"


def test_sources_have_valid_rss_urls():
    for name, specs in sources.SOURCES.items():
        assert specs, f"{name} 没有任何候选 RSS"
        for label, url in specs:
            assert label
            assert url.startswith("https://"), f"{name} 的 {label} 不是 HTTPS"
            assert "{" not in url or "{q}" in url


def test_data_sources_match_sources_keys():
    assert sources.DATA_SOURCES == list(sources.SOURCES)


def test_new_sources_registered_without_dead_twitter():
    assert "arXiv" in sources.SOURCES
    assert "InfoQ" in sources.SOURCES
    assert "Solidot" in sources.SOURCES
    assert "Twitter/X" not in sources.DATA_SOURCES
