"""轻量 .env 加载（不引入 python-dotenv）。

在项目根目录放一个 .env 即可：
    DEEPSEEK_API_KEY=sk-xxxx
    WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx

已存在的系统环境变量优先，不会被 .env 覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path


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
