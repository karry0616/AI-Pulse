r"""HTTP API：把 pipeline / database 的核心能力暴露给外部系统。

只用标准库 http.server，不引入额外依赖。默认监听 127.0.0.1:8502，
容器部署时用 --host 0.0.0.0 并通过反向代理对外。

启动：
    .\.venv\Scripts\python.exe api.py
自测：
    curl http://127.0.0.1:8502/api/health
    curl -X POST http://127.0.0.1:8502/api/run -H "Content-Type: application/json" -d "{\"keyword\":\"AI Agent\"}"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import database
import logging_setup
import pipeline
from sources import DATA_SOURCES, KEYWORDS

HOST = "127.0.0.1"
PORT = 8502
MAX_BODY_BYTES = 64 * 1024


def _log(message: str) -> None:
    logging_setup.log(message)


# 标记：本函数已负责落盘，pipeline 的 tee 层不必再写一遍
_log.writes_file = True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class APIHandler(BaseHTTPRequestHandler):
    server_version = "AI-Pulse/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # 响应辅助
    # ------------------------------------------------------------------
    def _send(self, code: int, body: dict | str) -> None:
        if isinstance(body, dict):
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        else:
            payload = str(body).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(payload)

    def _ok(self, **data) -> None:
        self._send(200, {"ok": True, **data})

    def _err(self, code: int, message: str) -> None:
        self._send(code, {"ok": False, "error": message})

    # ------------------------------------------------------------------
    # CORS 预检
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            routes = {
                "/api/health": lambda: self._ok(time=_now(), service="ai-pulse"),
                "/api/keywords": lambda: self._ok(keywords=KEYWORDS, sources=DATA_SOURCES),
                "/api/status": self._handle_status,
                "/api/alerts": lambda: self._handle_alerts(qs),
                "/api/threads": lambda: self._handle_threads(qs),
                "/api/intel": lambda: self._handle_intel(qs),
                "/api/runs": lambda: self._handle_runs(qs),
                "/api/cost": self._handle_cost,
                "/api/storage": lambda: self._ok(storage=database.storage_stats()),
            }
            handler = routes.get(path)
            if handler is None:
                self._err(404, f"未知路径：{path}")
                return
            handler()
        except Exception as exc:  # 单请求异常不影响服务
            _log(f"[WARN] GET {path} 失败：{type(exc).__name__}: {exc}")
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        try:
            body = self._read_json()
        except ValueError as exc:
            self._err(400, f"JSON 解析失败：{exc}")
            return

        try:
            routes = {
                "/api/run": lambda: self._handle_run(body),
                "/api/approve": lambda: self._handle_review(body),
                "/api/callback/feishu": lambda: self._handle_feishu_callback(body),
            }
            handler = routes.get(path)
            if handler is None:
                self._err(404, f"未知路径：{path}")
                return
            handler()
        except Exception as exc:
            _log(f"[WARN] POST {path} 失败：{type(exc).__name__}: {exc}")
            self._err(500, f"{type(exc).__name__}: {exc}")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"请求体过大（{length} 字节）")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------
    def _handle_status(self) -> None:
        self._ok(
            lock=pipeline.lock_status(),
            cooldown_seconds=pipeline.cooldown_window(),
            recent_runs=database.recent_runs(limit=3),
            keywords=KEYWORDS,
            sources=DATA_SOURCES,
        )

    def _handle_alerts(self, qs: dict) -> None:
        conditions = ["1=1"]
        params: list = []
        if qs.get("keyword"):
            conditions.append("keyword = ?")
            params.append(qs["keyword"][0])
        if qs.get("status"):
            conditions.append("status = ?")
            params.append(qs["status"][0])
        limit = min(int(qs.get("limit", ["20"])[0]), 200)

        sql = (
            "SELECT * FROM alerts WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id DESC LIMIT ?"
        )
        with database._connect() as conn:
            rows = conn.execute(sql, [*params, limit]).fetchall()
        self._ok(alerts=[dict(row) for row in rows])

    def _handle_threads(self, qs: dict) -> None:
        keyword = qs.get("keyword", [None])[0]
        days = int(qs.get("days", ["7"])[0])
        limit = min(int(qs.get("limit", ["20"])[0]), 100)
        self._ok(threads=database.top_threads(keyword=keyword, days=days, limit=limit))

    def _handle_intel(self, qs: dict) -> None:
        keyword = qs.get("keyword", [None])[0]
        days = int(qs.get("days", ["7"])[0])
        limit = min(int(qs.get("limit", ["100"])[0]), 500)
        df = database.fetch_recent(days=days, keyword=keyword)
        items = df.head(limit).to_dict("records") if not df.empty else []
        self._ok(items=items, total=int(len(df)))

    def _handle_runs(self, qs: dict) -> None:
        limit = min(int(qs.get("limit", ["10"])[0]), 100)
        self._ok(runs=database.recent_runs(limit=limit))

    def _handle_cost(self) -> None:
        with database._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)                            AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                       COALESCE(SUM(latency_ms), 0)        AS latency_ms
                  FROM llm_calls
                """
            ).fetchone()
        self._ok(cost=dict(row) if row else {})

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------
    def _handle_run(self, body: dict) -> None:
        keyword = str(body.get("keyword") or "").strip() or KEYWORDS[0]
        if keyword not in KEYWORDS:
            self._err(400, f"不支持的关键词：{keyword}，可选：{'、'.join(KEYWORDS)}")
            return

        sources = body.get("sources") or DATA_SOURCES
        sources = [name for name in sources if name in DATA_SOURCES]
        if not sources:
            self._err(400, "sources 为空或全部无效")
            return

        trigger = str(body.get("trigger") or "api")
        wait = bool(body.get("wait"))

        remaining = pipeline.cooldown_remaining(keyword)
        if remaining > 0:
            self._send(429, {
                "ok": False,
                "error": f"关键词「{keyword}」冷却中，还需等待 {remaining}s",
                "retry_after": remaining,
            })
            return

        if wait:
            try:
                result = pipeline.run(keyword, sources, trigger=trigger, log=_log)
            except pipeline.RunLockedError as exc:
                self._send(409, {"ok": False, "error": str(exc)})
                return
            self._ok(
                run_id=result.run_id,
                keyword=result.keyword,
                items=result.items,
                inserted=result.inserted,
                alert_id=result.alert_id,
                duration_ms=result.duration_ms,
                mode=(result.analysis or {}).get("_mode"),
                analysis=result.analysis,
            )
            return

        def background() -> None:
            try:
                pipeline.run(keyword, sources, trigger=trigger, log=_log)
            except pipeline.RunLockedError as exc:
                _log(f"[WARN] 后台 run 被锁挡住：{exc}")
            except Exception as exc:
                _log(f"[WARN] 后台 run 异常：{type(exc).__name__}: {exc}")

        threading.Thread(target=background, daemon=True).start()
        self._send(202, {
            "ok": True,
            "triggered": True,
            "keyword": keyword,
            "sources": sources,
            "trigger": trigger,
            "cooldown_seconds": pipeline.cooldown_window(),
        })

    def _handle_review(self, body: dict) -> None:
        alert_id = int(body.get("alert_id") or 0)
        action = str(body.get("action") or "approve").lower()
        reviewer = str(body.get("reviewed_by") or "api")
        note = str(body.get("review_note") or "")

        if not alert_id:
            self._err(400, "缺少 alert_id")
            return
        if action not in ("approve", "reject"):
            self._err(400, f"不支持的 action：{action}，可选 approve / reject")
            return
        if not database.get_alert(alert_id):
            self._err(404, f"预警 #{alert_id} 不存在")
            return

        if action == "approve":
            result = pipeline.push_alert(
                alert_id, log=_log, reviewed_by=reviewer, review_note=note
            )
            self._ok(
                alert_id=alert_id,
                action="approve",
                pushed=result.ok,
                channel=result.channel,
                detail=result.detail,
            )
            return

        outcome = pipeline.reject_alert(
            alert_id, log=_log, reviewed_by=reviewer, review_note=note
        )
        status = 200 if outcome.get("ok") else 409
        self._send(status, {"ok": bool(outcome.get("ok")), **outcome})

    def _handle_feishu_callback(self, body: dict) -> None:
        """飞书卡片按钮回调：点「通过」直接推送，点「驳回」留审计记录。

        兼容两种结构：
        - 老版：{"action": {"value": {...}}}
        - 卡片 2.0：{"event": {"action": {"value": {...}}, "operator": {"open_id": ...}}}
        """
        # 首次配置回调地址时飞书会发 verification 请求
        if body.get("type") == "url_verification":
            self._ok(challenge=body.get("challenge", ""))
            return

        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        action = event.get("action") or body.get("action") or {}
        value = action.get("value") if isinstance(action, dict) else {}
        value = value if isinstance(value, dict) else {}

        raw_id = value.get("alert_id") or value.get("id") or body.get("alert_id")
        try:
            alert_id = int(raw_id)
        except (TypeError, ValueError):
            self._err(400, "回调缺少有效的 alert_id")
            return

        decision = str(value.get("action") or value.get("decision") or "approve").lower()
        operator = (
            (event.get("operator") or {}).get("open_id")
            or (event.get("operator") or {}).get("name")
            or body.get("operator")
            or "feishu"
        )

        _log(f"[FEISHU] 卡片回调 #{alert_id} · {decision} · {operator}")

        if decision in ("reject", "rejected"):
            outcome = pipeline.reject_alert(
                alert_id, log=_log, reviewed_by=str(operator), review_note="飞书卡片驳回"
            )
            self._send(200 if outcome.get("ok") else 409, {
                "toast": {"type": "success" if outcome.get("ok") else "warning",
                          "content": outcome.get("reason")},
                **outcome,
            })
            return

        result = pipeline.push_alert(
            alert_id, log=_log, reviewed_by=str(operator), review_note="飞书卡片审批"
        )
        self._send(200, {
            "toast": {"type": "success" if result.ok else "error",
                      "content": f"预警 #{alert_id} {'已推送' if result.ok else '推送失败'}"},
            "ok": result.ok,
            "alert_id": alert_id,
            "detail": result.detail,
        })

    # ------------------------------------------------------------------
    # 访问日志
    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        _log(f"[HTTP] {fmt % args}")


class PulseServer(ThreadingHTTPServer):
    """客户端提前断开（浏览器刷新、curl 超时）属正常现象，不打印回溯。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def create_server(host: str = HOST, port: int = PORT) -> PulseServer:
    return PulseServer((host, port), APIHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse HTTP API")
    parser.add_argument("--host", default=os.getenv("API_HOST", HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", PORT)))
    args = parser.parse_args()

    config.load_env()
    database.init_db()

    logging_setup.setup()
    server = create_server(args.host, args.port)
    base = f"http://{args.host}:{args.port}"
    _log(f"[API] 服务已启动：{base}")
    _log("[API] 端点：/api/health /api/status /api/run /api/alerts /api/approve")
    _log("[API]       /api/threads /api/intel /api/runs /api/cost /api/storage")
    _log(f"[API] 冷却：同一关键词 {pipeline.cooldown_window()}s 内只允许触发一次")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("[API] 服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
