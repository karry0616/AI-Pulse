"""ChromaDB 本地向量记忆：过去 7 天资讯的入库与召回。

嵌入向量在本地用字符 n-gram 哈希生成（不依赖联网下载模型）；
Chroma 初始化失败时自动降级为关键词重合度检索，保证主流程不中断。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import database

CHROMA_DIR = Path(__file__).resolve().parent.parent / "chroma_db"
COLLECTION_NAME = "intel_history"
EMBED_DIM = 512
RETRY_COOLDOWN = 300  # 向量库失败后的冷却秒数，到期自动重试

_collection = None
_disabled_until = 0.0


def _disable(reason: str, log=print) -> None:
    global _collection, _disabled_until
    _disabled_until = time.time() + RETRY_COOLDOWN
    _collection = None
    log(
        f"[RAG] 向量库不可用，降级关键词检索"
        f"（{RETRY_COOLDOWN // 60} 分钟后重试）：{reason[:120]}"
    )


def _get_collection(log=print):
    global _collection, _disabled_until
    if _collection is not None:
        return _collection
    if _disabled_until and time.time() < _disabled_until:
        return None

    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        _disabled_until = 0.0
        return _collection
    except Exception as exc:  # 依赖缺失、目录权限、嵌入模型初始化失败
        _disable(f"{type(exc).__name__}: {exc}", log)
        return None


def _doc_id(item: dict) -> str:
    raw = f"{item.get('date', '')}|{item.get('title', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _doc_text(item: dict) -> str:
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    return f"{title}。{summary}"[:600]


def _embed_text(text: str) -> list[float]:
    """字符 1/2-gram 哈希嵌入：离线可用，中英文都适用。"""
    cleaned = re.sub(r"[\s\W_]+", "", str(text).lower())
    grams: Counter[str] = Counter(cleaned)
    for i in range(len(cleaned) - 1):
        grams[cleaned[i : i + 2]] += 1

    vector = [0.0] * EMBED_DIM
    for gram, count in grams.items():
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest()
        bucket = int.from_bytes(digest, "big") % EMBED_DIM
        vector[bucket] += 1.0 + math.log(count)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def _embed_many(texts: list[str]) -> list[list[float]]:
    return [_embed_text(text) for text in texts]


def _metadata(item: dict) -> dict:
    day = str(item.get("date") or date.today().isoformat())
    try:
        day_index = date.fromisoformat(day).toordinal()
    except ValueError:
        day_index = date.today().toordinal()
    return {
        "date": day,
        "day_index": day_index,
        "keyword": str(item.get("keyword") or ""),
        "source": str(item.get("source") or ""),
        "sentiment": str(item.get("sentiment") or "neutral"),
        "title": str(item.get("title") or ""),
    }


def count() -> int:
    collection = _get_collection()
    if collection is None:
        return 0
    try:
        return int(collection.count())
    except Exception:
        return 0


def index_items(items: list[dict], log=print) -> int:
    """把资讯 upsert 进向量库，返回写入条数。"""
    collection = _get_collection(log)
    if collection is None:
        return 0

    rows = [item for item in items if item.get("title")]
    if not rows:
        return 0

    ids = [_doc_id(item) for item in rows]
    documents = [_doc_text(item) for item in rows]
    metadatas = [_metadata(item) for item in rows]

    try:
        collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=_embed_many(documents),
        )
    except Exception as exc:
        _disable(f"{type(exc).__name__}: {exc}", log)
        return 0

    log(f"[RAG] 向量入库 {len(ids)} 条（累计 {collection.count()} 条）")
    return len(ids)


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", str(text).lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _fallback_query(texts: list[str], k: int, days: int, log=print) -> list[dict]:
    """无向量能力时，用字符二元组重合度做替代检索。"""
    df = database.fetch_recent(days=days)
    if df.empty:
        return []

    query_grams: set[str] = set()
    for text in texts:
        query_grams |= _bigrams(text)

    scored: list[tuple[float, dict]] = []
    for row in df.to_dict("records"):
        doc_grams = _bigrams(f"{row.get('title', '')} {row.get('summary', '')}")
        if not doc_grams or not query_grams:
            continue
        overlap = len(query_grams & doc_grams)
        score = overlap / len(query_grams | doc_grams)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "title": row.get("title", ""),
                    "date": row.get("date", ""),
                    "source": row.get("source", ""),
                    "sentiment": row.get("sentiment", "neutral"),
                    "similarity": round(score, 3),
                    "mode": "keyword",
                },
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = [item for _, item in scored[:k]]
    if results:
        log(f"[RAG] 关键词召回 {len(results)} 条历史资讯（降级模式）")
    return results


def query_similar(
    texts: list[str],
    k: int = 6,
    days: int = 7,
    log=print,
) -> list[dict]:
    """召回近 N 天相似资讯；向量不可用时自动降级。"""
    if not texts:
        return []

    collection = _get_collection(log)
    if collection is None:
        return _fallback_query(texts, k, days, log)

    try:
        total = int(collection.count())
        if total == 0:
            return _fallback_query(texts, k, days, log)

        cutoff = (date.today() - timedelta(days=days)).toordinal()
        payload = collection.query(
            query_embeddings=_embed_many(texts[:5]),
            n_results=min(k, total),
            where={"day_index": {"$gte": cutoff}},
        )
    except Exception as exc:
        _disable(f"{type(exc).__name__}: {exc}", log)
        return _fallback_query(texts, k, days, log)

    documents = (payload.get("documents") or [[]])[0]
    metadatas = (payload.get("metadatas") or [[]])[0]
    distances = (payload.get("distances") or [[]])[0]

    results: list[dict] = []
    seen: set[str] = set()
    for doc, meta, distance in zip(documents, metadatas, distances):
        meta = meta or {}
        title = str(meta.get("title") or doc[:40])
        if title in seen:
            continue
        seen.add(title)
        results.append(
            {
                "title": title,
                "date": meta.get("date", ""),
                "source": meta.get("source", ""),
                "sentiment": meta.get("sentiment", "neutral"),
                "similarity": round(1 - float(distance), 3),
                "mode": "vector",
            }
        )

    if results:
        log(f"[RAG] 向量召回 {len(results)} 条历史资讯")
    return results


def backfill(days: int = 7, log=print) -> int:
    """把 SQLite 里的历史资讯补进向量库（幂等）。"""
    df = database.fetch_recent(days=days)
    if df.empty:
        log("[RAG] 没有可回填的历史资讯")
        return 0
    return index_items(df.to_dict("records"), log)


def main() -> int:
    parser = argparse.ArgumentParser(description="ChromaDB 记忆自测")
    parser.add_argument("--backfill", type=int, default=0, help="回填最近 N 天")
    parser.add_argument("--query", default="", help="检索关键词/句子")
    args = parser.parse_args()

    import config

    config.load_env()
    database.init_db()

    def log(message: str) -> None:
        print(message)

    if args.backfill:
        log(f"[RAG] 回填 {args.backfill} 天，写入 {backfill(args.backfill, log)} 条")
    if args.query:
        log(f"[RAG] 当前向量总数：{count()}")
        for item in query_similar([args.query], k=5, log=log):
            log(f"  {item['similarity']:.2f} [{item['date']}] {item['title'][:60]}")
    if not args.backfill and not args.query:
        log(f"[RAG] 当前向量总数：{count()}（目录 {CHROMA_DIR}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
