"""统一日志：输出到 logs/mcp_search.log，滚动保留。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _app_dir() -> Path:
    """应用基准目录：PyInstaller frozen 时为 exe 所在目录，否则为源码目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LOG_DIR = _app_dir() / "logs"
LOG_FILE = LOG_DIR / "mcp_search.log"
_LOGGER_NAME = "mcp_search"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化并返回项目 logger（幂等，可重复调用）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    LOG_DIR.mkdir(exist_ok=True)
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


def mask_key(api_key: str) -> str:
    """API Key 脱敏：仅保留前 8 位，禁止完整密钥进入日志。"""
    if not api_key:
        return "<empty>"
    return f"{api_key[:8]}***"
