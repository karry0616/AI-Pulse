"""事件线归并测试：全部走临时库，不触碰真实 pulse.db。"""

from datetime import date

import pytest

import database
import events


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()


def item(title, sentiment="neutral", source="RSS", keyword="AI Agent"):
    return {
        "date": date.today().isoformat(),
        "keyword": keyword,
        "title": title,
        "summary": "摘要",
        "sentiment": sentiment,
        "source": source,
    }


def analysis(events_detail=None):
    return {"_events_detail": events_detail or []}


def test_similar_titles_merge_into_one_thread():
    first = [item("OpenAI 发布 GPT-5 模型，推理能力大幅提升", "positive", "TechCrunch")]
    second = [item("OpenAI发布GPT-5模型：推理能力大幅提升", "positive", "36Kr")]

    events.link_run(first, analysis(), "AI Agent", log=lambda *_: None)
    summary = events.link_run(second, analysis(), "AI Agent", log=lambda *_: None)

    assert summary["linked"] == 1
    assert summary["new"] == 0
    threads = database.top_threads("AI Agent", days=7)
    assert len(threads) == 1
    assert threads[0]["item_count"] == 2
    assert len(database.thread_timeline(threads[0]["id"])) == 2


def test_unrelated_titles_create_separate_threads():
    rows = [
        item("OpenAI 发布 GPT-5 模型，推理能力大幅提升"),
        item("英伟达数据中心营收连续五个季度创新高"),
    ]
    summary = events.link_run(rows, analysis(), "AI Agent", log=lambda *_: None)

    assert summary["linked"] == 2
    assert summary["new"] == 2
    assert len(database.top_threads("AI Agent", days=7)) == 2


def test_entity_hit_lowers_similarity_threshold():
    first = [item("OpenAI 发布 GPT-5 模型，推理能力大幅提升，开发者反响热烈")]
    detail_first = [{"title": first[0]["title"], "sentiment": "positive", "entities": ["OpenAI"]}]
    events.link_run(first, analysis(detail_first), "AI Agent", log=lambda *_: None)

    # 仅标题相似度不足以归并，靠 Collector 实体命中加成
    second = [item("OpenAI GPT-5 推理能力提升")]
    detail = [{"title": second[0]["title"], "sentiment": "positive", "entities": ["OpenAI"]}]
    summary = events.link_run(second, analysis(detail), "AI Agent", log=lambda *_: None)

    assert summary["new"] == 0
    threads = database.top_threads("AI Agent", days=7)
    assert len(threads) == 1
    assert threads[0]["item_count"] == 2
    assert threads[0]["entity"] == "OpenAI"


def test_same_entity_different_events_do_not_collide():
    rows = [
        item("OpenAI 完成新一轮融资，估值再创新高"),
        item("OpenAI 遭遇版权诉讼，多家出版社联合起诉"),
    ]
    summary = events.link_run(rows, analysis(), "AI Agent", log=lambda *_: None)

    assert summary["new"] == 2
    assert len(database.top_threads("AI Agent", days=7)) == 2


def test_relink_is_idempotent():
    rows = [item("OpenAI 发布 GPT-5 模型，推理能力大幅提升")]
    events.link_run(rows, analysis(), "AI Agent", log=lambda *_: None)
    again = events.link_run(rows, analysis(), "AI Agent", log=lambda *_: None)

    assert again["linked"] == 0
    assert again["new"] == 0
    thread = again["top"][0]
    assert thread["item_count"] == 1
    assert len(database.thread_timeline(thread["id"])) == 1
