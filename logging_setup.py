"""统一日志：控制台 + 按大小轮转的文件（logs/pulse.log）。

各入口（API / 调度器 / pipeline CLI / Streamlit）调用 setup() 后，
用 log() 写日志即可同时落盘，进程退出后仍可追溯。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.getenv("PULSE_LOG_DIR") or (Path(__file__).resolve().parent / "logs"))
LOG_FILE = LOG_DIR / "pulse.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False


def setup(level: int = logging.INFO) -> logging.Logger:
    """初始化 pulse logger：控制台 + 轮转文件，重复调用只生效一次。"""
    global _configured
    logger = logging.getLogger("pulse")
    if _configured:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        _configured = True
    except OSError as exc:  # 只读目录等场景：退化为控制台日志，不阻断主流程
        logger.warning("日志文件不可写，仅输出到控制台：%s", exc)
        _configured = True

    return logger


def log(message: str) -> None:
    """按消息前缀决定级别，统一写入控制台与文件。"""
    text = str(message)
    level = logging.WARNING if text.lstrip().startswith("[WARN]") else logging.INFO
    setup().log(level, text)


def log_file() -> Path:
    return LOG_FILE
