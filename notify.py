"""预警推送：飞书交互卡片 / 钉钉 Markdown。

由 pipeline 在人工审核通过后调用，不在抓取流程里自动触发。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote_plus

import requests

import config

WEBHOOK_TIMEOUT = 10


@dataclass
class PushResult:
    ok: bool
    channel: str
    detail: str = ""


def webhook_url() -> str:
    config.load_env()
    return os.getenv("WEBHOOK_URL", "").strip()


def webhook_configured() -> bool:
    return bool(webhook_url())


def channel_name(url: str | None = None) -> str:
    target = (url or webhook_url()).lower()
    if "dingtalk" in target:
        return "钉钉"
    if "feishu" in target or "larksuite" in target:
        return "飞书"
    return "Webhook"


def public_base_url() -> str:
    """公网回调地址（飞书卡片按钮使用）；未配置则卡片不带按钮。"""
    config.load_env()
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def callback_ready(url: str | None = None) -> bool:
    return bool(public_base_url()) and channel_name(url) == "飞书"



def build_markdown(warning_text: str, keyword: str, heat_score: int | None) -> str:
    heat_line = f"\n\n**舆情热度**：{heat_score}" if heat_score else ""
    return (
        f"**AI Pulse 风险预警**\n\n"
        f"**监控关键词**：{keyword}\n\n"
        f"{warning_text}{heat_line}\n\n"
        f"> 数据来源：AI Pulse RSS 情报监控 · {datetime.now():%Y-%m-%d %H:%M}"
    )


def dingtalk_secret() -> str:
    config.load_env()
    return os.getenv("DINGTALK_SECRET", "").strip()


def sign_dingtalk(url: str, secret: str) -> str:
    """钉钉自定义机器人「加签」：拼接 timestamp + sign 查询参数。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={timestamp}&sign={sign}"


def _action_block(alert_id: int) -> dict:
    """飞书卡片底部审批按钮：点击后回调 /api/callback/feishu。"""
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "通过并推送"},
                "type": "primary",
                "value": {"action": "approve", "alert_id": str(alert_id)},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "驳回"},
                "type": "danger",
                "value": {"action": "reject", "alert_id": str(alert_id)},
            },
        ],
    }


def _payload(target: str, markdown: str, alert_id: int | None = None) -> dict:
    if "dingtalk" in target.lower():
        return {
            "msgtype": "markdown",
            "markdown": {"title": "AI Pulse 风险预警", "text": markdown},
        }

    elements: list[dict] = [{"tag": "markdown", "content": markdown}]
    if alert_id and callback_ready(target):
        elements.append(_action_block(alert_id))
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"> 点击按钮即可完成审批，回调地址：{public_base_url()}/api/callback/feishu\n"
                    f"> 预警编号 #{alert_id}"
                ),
            }
        )

    # 飞书交互卡片；其他测试端点也按这个格式发送
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "⚠️ AI Pulse 风险预警"},
            },
            "elements": elements,
        },
    }


def send_webhook(
    warning_text: str,
    keyword: str = "AI Agent",
    heat_score: int | None = None,
    url: str | None = None,
    alert_id: int | None = None,
    log=print,
) -> PushResult:
    """把预警推送到 Webhook，返回结果对象。"""
    target = (url or webhook_url()).strip()
    channel = channel_name(target)

    if not target:
        log("[WARN] WEBHOOK_URL 未配置，跳过推送")
        return PushResult(False, channel, "WEBHOOK_URL 未配置")

    secret = dingtalk_secret()
    if channel == "钉钉" and secret:
        target = sign_dingtalk(target, secret)

    markdown = build_markdown(warning_text, keyword, heat_score)

    try:
        resp = requests.post(
            target, json=_payload(target, markdown, alert_id), timeout=WEBHOOK_TIMEOUT
        )
    except requests.RequestException as exc:
        log(f"[WARN] Webhook 推送失败：{type(exc).__name__}")
        return PushResult(False, channel, type(exc).__name__)

    try:
        body = resp.json()
    except ValueError:
        body = {}

    codes = [body.get(key) for key in ("code", "errcode", "StatusCode") if key in body]
    ok = resp.status_code == 200 and all(code == 0 for code in codes)

    if ok:
        log(f"[OK] Webhook 推送成功（{channel}）")
        return PushResult(True, channel, f"HTTP {resp.status_code}")

    log(f"[WARN] Webhook 推送失败：HTTP {resp.status_code}，响应 {body}")
    return PushResult(False, channel, f"HTTP {resp.status_code} {body}")
