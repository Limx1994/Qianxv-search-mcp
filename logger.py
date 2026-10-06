"""统一日志：按进程输出到 logs/mcp_search_<PID>.log，滚动保留。"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _app_dir() -> Path:
    """应用基准目录：PyInstaller frozen 时为 exe 所在目录，否则为源码目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LOG_DIR = _app_dir() / "logs"
# 各进程独立轮转，避免并发运行时争用同一日志文件。
LOG_FILE = LOG_DIR / f"mcp_search_{os.getpid()}.log"
_LOGGER_NAME = "mcp_search"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化并返回项目 logger（幂等，可重复调用）。"""
    global LOG_DIR, LOG_FILE
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        # 多模块重复初始化时复用已有 handler，避免同一条日志被重复写入。
        return logger
    logger.setLevel(level)
    try:
        LOG_DIR.mkdir(exist_ok=True)
    except OSError:
        # 应用目录不可写时使用临时目录，日志文件仍按进程隔离。
        import tempfile
        LOG_DIR = Path(tempfile.gettempdir()) / "qianxv-search-mcp-logs"
        LOG_DIR.mkdir(exist_ok=True)
        LOG_FILE = LOG_DIR / f"mcp_search_{os.getpid()}.log"
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    # 抑制 httpx INFO 级请求日志，避免噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logger
