"""轻量 .env 加载（不引入 python-dotenv）。

在项目根目录放一个 .env 即可：
    DEEPSEEK_API_KEY=sk-xxxx
    WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx

已存在的系统环境变量优先，不会被 .env 覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

# 启动自检：必填缺失直接告警，可选缺失只提示降级
REQUIRED_KEYS = ("DEEPSEEK_API_KEY",)
OPTIONAL_KEYS = ("WEBHOOK_URL", "DINGTALK_SECRET", "PUBLIC_BASE_URL")


def load_env(path: str | Path | None = None) -> None:
    env_path = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        # 系统环境变量优先；但进程里已有的空值（如启动时设了 ""）允许被 .env 覆盖
        if os.environ.get(key):
            continue
        os.environ[key] = value


def mask_secret(value: str) -> str:
    """日志与页面只展示掩码，避免密钥明文泄漏。"""
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-3:]}"


def secret_status() -> dict[str, dict]:
    """各密钥的配置情况（只返回掩码，不返回明文）。"""
    load_env()
    result: dict[str, dict] = {}
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        value = os.getenv(key, "").strip()
        result[key] = {
            "configured": bool(value),
            "required": key in REQUIRED_KEYS,
            "masked": mask_secret(value),
        }
    return result


def missing_secrets() -> list[str]:
    """缺失的必填密钥列表。"""
    return [key for key, info in secret_status().items() if info["required"] and not info["configured"]]


def secret_warnings() -> list[str]:
    """给页面侧边栏用的启动自检提示。"""
    status = secret_status()
    warnings: list[str] = []
    if not status["DEEPSEEK_API_KEY"]["configured"]:
        warnings.append("⚠️ 未配置 DEEPSEEK_API_KEY，将走本地规则引擎")
    if not status["WEBHOOK_URL"]["configured"]:
        warnings.append("⚠️ 未配置 WEBHOOK_URL，预警审批后无法推送")
    elif not status["PUBLIC_BASE_URL"]["configured"]:
        warnings.append("ℹ️ 未配置 PUBLIC_BASE_URL，飞书卡片不带审批按钮")
    return warnings
