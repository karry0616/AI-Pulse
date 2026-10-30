"""RAG 测试：离线嵌入、文档 ID 幂等、向量库失败后的冷却与恢复。"""

import math
import sys
import types
from datetime import date

import database
from rag import store


def test_embed_text_is_normalized():
    vector = store._embed_text("AI Agent 发布新能力")
    assert len(vector) == store.EMBED_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)


def test_embed_text_is_deterministic():
    assert store._embed_text("大模型推理") == store._embed_text("大模型推理")


def test_doc_id_depends_on_date_and_title():
    a = store._doc_id({"date": "2026-01-01", "title": "新闻A"})
    b = store._doc_id({"date": "2026-01-01", "title": "新闻A"})
    c = store._doc_id({"date": "2026-01-02", "title": "新闻A"})
    assert a == b
    assert a != c


def test_fallback_query_filters_by_keyword(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rag.db")
    database.init_db()
    day = date.today().isoformat()
    database.save_items(
        [
            {
                "date": day,
                "keyword": "AI Agent",
                "title": "OpenAI 发布新模型",
                "summary": "通用能力升级",
                "sentiment": "neutral",
                "source": "test",
                "url": "",
            },
            {
                "date": day,
                "keyword": "RAG",
                "title": "OpenAI 发布新模型 RAG 专场",
                "summary": "检索增强专场",
                "sentiment": "neutral",
                "source": "test",
                "url": "",
            },
        ]
    )

    results = store._fallback_query(
        ["OpenAI 发布新模型 RAG 专场"], k=5, days=7, log=lambda m: None, keyword="AI Agent"
    )
    assert [item["title"] for item in results] == ["OpenAI 发布新模型"]


def test_collection_recovers_after_cooldown(monkeypatch):
    monkeypatch.setattr(store, "_collection", None)
    monkeypatch.setattr(store, "_disabled_until", 0.0)

    class BrokenClient:
        def __init__(self, **kwargs):
            raise RuntimeError("chroma down")

    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(PersistentClient=BrokenClient))
    assert store._get_collection(log=lambda m: None) is None
    assert store._disabled_until > 0

    # 冷却期内不再尝试，直接返回 None
    monkeypatch.setattr(store.time, "time", lambda: store._disabled_until - 1)
    assert store._get_collection(log=lambda m: None) is None

    # 冷却期满后重新初始化，这一次成功
    working = types.SimpleNamespace(
        PersistentClient=lambda **kwargs: types.SimpleNamespace(
            get_or_create_collection=lambda **kwargs: object()
        )
    )
    monkeypatch.setitem(sys.modules, "chromadb", working)
    monkeypatch.setattr(store.time, "time", lambda: store._disabled_until + 1)
    assert store._get_collection(log=lambda m: None) is not None
    assert store._disabled_until == 0.0
