"""DeepSeek 客户端：JSON Mode 调用 + token / 耗时计量。

所有 Agent 节点的模型调用都走这里，便于统一记录成本（llm_calls 表）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import requests

import config
import database

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
API_TIMEOUT = 60
MAX_RETRIES = 2
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = 1.0


class LLMUnavailable(RuntimeError):
    """未配置 API Key 或网络不可用，调用方应降级。"""


class LLMCallError(RuntimeError):
    """API 返回非 200 或响应结构异常。"""


@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def available() -> bool:
    config.load_env()
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


def chat(
    messages: list[dict],
    *,
    node: str,
    run_id: str = "-",
    log=print,
    temperature: float = 0.2,
    max_tokens: int = 1200,
    json_mode: bool = True,
) -> ChatResult:
    """调用 DeepSeek 并记录这一次调用的 token 与耗时。"""
    config.load_env()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise LLMUnavailable("未设置环境变量 DEEPSEEK_API_KEY")

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    resp = None
    attempts = 0
    for attempt in range(MAX_RETRIES + 1):
        attempts = attempt + 1
        try:
            resp = requests.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=API_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS * (2 ** attempt))
                continue
            latency_ms = int((time.perf_counter() - started) * 1000)
            database.log_llm_call(run_id, node, MODEL, 0, 0, latency_ms, ok=False,
                                  error=f"{type(exc).__name__} after {attempts} attempts")
            raise LLMUnavailable(f"DeepSeek 网络异常：{type(exc).__name__}") from exc

        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            time.sleep(BACKOFF_SECONDS * (2 ** attempt))
            continue
        break

    latency_ms = int((time.perf_counter() - started) * 1000)

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:120]
        database.log_llm_call(run_id, node, MODEL, 0, 0, latency_ms, ok=False,
                              error=f"HTTP {resp.status_code} after {attempts} attempts")
        raise LLMCallError(f"DeepSeek HTTP {resp.status_code}: {detail}")

    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        database.log_llm_call(run_id, node, MODEL, 0, 0, latency_ms, ok=False,
                              error="响应结构异常")
        raise LLMCallError("DeepSeek 响应结构异常") from exc

    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    database.log_llm_call(
        run_id, node, body.get("model") or MODEL,
        prompt_tokens, completion_tokens, latency_ms,
    )
    log(
        f"[COST] {node} {prompt_tokens}+{completion_tokens} tok · "
        f"{latency_ms / 1000:.1f}s"
    )
    return ChatResult(
        content=content,
        model=body.get("model") or MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )


def extract_json(raw: str) -> dict | None:
    """剥离代码块包裹并提取第一个合法 JSON 对象，失败返回 None。"""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 客户端自测")
    parser.add_argument("--prompt", default='返回 JSON：{"ok": true}')
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    def log(message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}")

    if not available():
        log("[WARN] 未配置 DEEPSEEK_API_KEY，无法做真实调用自测")
        return 1

    try:
        result = chat(
            [{"role": "user", "content": args.prompt}],
            node="self_test",
            run_id="self_test",
            log=log,
        )
    except (LLMUnavailable, LLMCallError) as exc:
        log(f"[WARN] {exc}")
        return 1

    log(f"prompt={result.prompt_tokens} completion={result.completion_tokens} "
        f"latency={result.latency_ms}ms")
    print(result.content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
