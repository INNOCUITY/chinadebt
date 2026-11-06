"""结构化日志。

- 默认输出到 stderr，便于在 CLI 中与 stdout（数据/表格）分离。
- 级别由环境变量 `FICC_LOG_LEVEL` 控制（DEBUG/INFO/WARNING/ERROR）。
- 可选写入文件（`FICC_LOG_FILE`），用于留痕长任务。
"""

from __future__ import annotations

import logging
import os
import sys

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def setup_logging(level: str | None = None, log_file: str | None = None) -> None:
    """初始化根 logger（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.environ.get("FICC_LOG_LEVEL", "INFO")).upper()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stderr)]
    path = log_file or os.environ.get("FICC_LOG_FILE")
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO), format=_FMT, datefmt=_DATEFMT, handlers=handlers
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger。"""
    setup_logging()
    return logging.getLogger(name)
