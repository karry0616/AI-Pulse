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
import hashlib
import hmac
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
from rag import store as rag_store
from sources import DATA_SOURCES, KEYWORDS

HOST = "127.0.0.1"
PORT = 8502
MAX_BODY_BYTES = 64 * 1024
# 健康检查与飞书回调不要求 API Token（后者靠飞书签名校验）
PUBLIC_PATHS = {"/api/health", "/api/callback/feishu"}


def _log(message: str) -> None:
    logging_setup.log(message)


# 标记：本函数已负责落盘，pipeline 的 tee 层不必再写一遍
_log.writes_file = True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _api_token() -> str:
    config.load_env()
    return os.getenv("API_TOKEN", "").strip()


def _int_param(qs: dict, name: str, default: int, maximum: int, minimum: int = 1) -> int:
    """查询参数取整数并夹紧范围；非法值抛 ValueError（由路由转成 400）。"""
    raw = (qs.get(name) or [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {name} 必须是整数，实际为 {raw!r}") from None
    if value < minimum:
        raise ValueError(f"参数 {name} 不能小于 {minimum}")
    return min(value, maximum)


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
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(payload)

    def _ok(self, **data) -> None:
        self._send(200, {"ok": True, **data})

    def _err(self, code: int, message: str) -> None:
        self._send(code, {"ok": False, "error": message})

    # ------------------------------------------------------------------
    # 鉴权与 CORS
    # ------------------------------------------------------------------
    def _cors_origin(self) -> str:
        """默认不发 CORS 头；只有显式配置 CORS_ALLOW_ORIGIN 才允许跨域。"""
        config.load_env()
        allowed = os.getenv("CORS_ALLOW_ORIGIN", "").strip()
        if not allowed:
            return ""
        if allowed == "*":
            return "*"
        origin = (self.headers.get("Origin") or "").strip()
        candidates = {item.strip() for item in allowed.split(",") if item.strip()}
        return origin if origin in candidates else ""

    def _authorized(self, path: str) -> bool:
        token = _api_token()
        if not token or path in PUBLIC_PATHS:
            return True

        provided = (self.headers.get("X-API-Token") or "").strip()
        if not provided:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        return bool(provided) and hmac.compare_digest(provided, token)

    # ------------------------------------------------------------------
    # CORS 预检
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-API-Token, Authorization"
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if not self._authorized(path):
            self._err(401, "缺少或无效的 API Token（请求头 X-API-Token / Authorization: Bearer）")
            return

        try:
            routes = {
                "/api/health": self._handle_health,
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
        except ValueError as exc:
            self._err(400, str(exc))
        except Exception as exc:  # 单请求异常不影响服务
            _log(f"[WARN] GET {path} 失败：{type(exc).__name__}: {exc}")
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if not self._authorized(path):
            self._err(401, "缺少或无效的 API Token（请求头 X-API-Token / Authorization: Bearer）")
            return

        self._raw_body = b""
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
        except ValueError as exc:
            self._err(400, str(exc))
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
        self._raw_body = raw
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------
    def _handle_health(self) -> None:
        """就绪检查：数据库可用才算健康；向量库与运行锁只作状态上报。"""
        db = database.health()
        payload = {
            "time": _now(),
            "db": db,
            "rag": rag_store.status(),
            "lock": pipeline.lock_status(),
        }
        if not db.get("ok"):
            self._send(503, {"ok": False, "error": "数据库不可用", **payload})
            return
        self._ok(**payload)

    def _handle_status(self) -> None:
        self._ok(
            lock=pipeline.lock_status(),
            cooldown_seconds=pipeline.cooldown_window(),
            recent_runs=database.recent_runs(limit=3),
            keywords=KEYWORDS,
            sources=DATA_SOURCES,
        )

    def _handle_alerts(self, qs: dict) -> None:
        keyword = qs.get("keyword", [None])[0]
        status = qs.get("status", [None])[0]
        limit = _int_param(qs, "limit", 20, 200)
        self._ok(
            alerts=database.recent_alerts(limit=limit, keyword=keyword, status=status)
        )

    def _handle_threads(self, qs: dict) -> None:
        keyword = qs.get("keyword", [None])[0]
        days = _int_param(qs, "days", 7, 90)
        limit = _int_param(qs, "limit", 20, 100)
        self._ok(threads=database.top_threads(keyword=keyword, days=days, limit=limit))

    def _handle_intel(self, qs: dict) -> None:
        keyword = qs.get("keyword", [None])[0]
        days = _int_param(qs, "days", 7, 90)
        limit = _int_param(qs, "limit", 100, 500)
        df = database.fetch_recent(days=days, keyword=keyword)
        items = df.head(limit).to_dict("records") if not df.empty else []
        self._ok(items=items, total=int(len(df)))

    def _handle_runs(self, qs: dict) -> None:
        limit = _int_param(qs, "limit", 10, 100)
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

        # 异步触发前先看运行锁：已有任务在跑就立刻返回 409，避免 202 后静默失败
        lock = pipeline.lock_status()
        if lock and lock.get("alive"):
            self._send(409, {"ok": False, "error": "已有任务正在运行", "lock": lock})
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
        raw_id = body.get("alert_id")
        try:
            alert_id = int(raw_id)
        except (TypeError, ValueError):
            self._err(400, "缺少有效的 alert_id（必须是整数）")
            return
        if alert_id <= 0:
            self._err(400, "缺少有效的 alert_id（必须是正整数）")
            return

        action = str(body.get("action") or "approve").lower()
        reviewer = str(body.get("reviewed_by") or "api")
        note = str(body.get("review_note") or "")
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

    def _verify_feishu(self, body: dict) -> bool:
        """校验飞书回调签名与 verification token（未配置则跳过，便于本地联调）。

        签名算法：sha256(timestamp + nonce + encrypt_key + 原始请求体)。
        时间戳必须来自原始 header，因此需要 _read_json 保存的 _raw_body。
        """
        config.load_env()
        token = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()
        encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "").strip()

        if token:
            header = body.get("header") if isinstance(body.get("header"), dict) else {}
            provided = str(body.get("token") or header.get("token") or "")
            if provided and not hmac.compare_digest(provided, token):
                return False

        if encrypt_key:
            timestamp = (self.headers.get("X-Lark-Request-Timestamp") or "").strip()
            nonce = (self.headers.get("X-Lark-Request-Nonce") or "").strip()
            signature = (self.headers.get("X-Lark-Signature") or "").strip()
            if not timestamp or not nonce or not signature:
                return False
            content = (timestamp + nonce + encrypt_key).encode("utf-8") + (
                self._raw_body or b""
            )
            expected = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return False

        return True

    def _handle_feishu_callback(self, body: dict) -> None:
        """飞书卡片按钮回调：点「通过」直接推送，点「驳回」留审计记录。

        兼容两种结构：
        - 老版：{"action": {"value": {...}}}
        - 卡片 2.0：{"event": {"action": {"value": {...}}, "operator": {"open_id": ...}}}
        """
        if not self._verify_feishu(body):
            _log("[FEISHU] 回调签名校验失败，已拒绝")
            self._err(403, "飞书回调签名校验失败")
            return

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


def bootstrap_db() -> str | None:
    """初始化数据库；失败时返回原因串而不是抛出。

    数据库不可用（路径不可写、磁盘满、文件被占用）时服务仍要起来：
    /api/health 会返回 503 并带上原因，容器探针与外部监控才有信号可看。
    直接退出进程会让「为什么挂了」变成一个只能翻日志的谜。
    """
    try:
        database.init_db()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Pulse HTTP API")
    parser.add_argument("--host", default=os.getenv("API_HOST", HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", PORT)))
    args = parser.parse_args()

    config.load_env()
    logging_setup.setup()

    failure = bootstrap_db()
    if failure:
        _log(f"[WARN] 数据库初始化失败，服务以降级模式启动：{failure}")
        _log("[WARN] /api/health 将返回 503，其余接口会陆续报 500，请先修复数据库")
    else:
        _log(f"[API] 数据库就绪：{database.db_path()}")

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
