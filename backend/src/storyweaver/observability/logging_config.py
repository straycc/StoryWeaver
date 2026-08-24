"""StoryWeaver 标准库日志配置。"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .context import get_log_context


LOGGER_NAME = "storyweaver"
_CONFIGURATION_LOCK = threading.RLock()
_CONTEXT_FIELDS = (
    "request_id",
    "session_id",
    "book_id",
    "run_id",
    "action",
    "agent_id",
)

# 库被测试或嵌入使用、但尚未调用 configure_logging 时不擅自输出。
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


class _ContextFormatter(logging.Formatter):
    """在普通日志后追加当前调用链的结构化字段。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        context = get_log_context()
        fields = " ".join(
            f"{name}={context[name]}"
            for name in _CONTEXT_FIELDS
            if context.get(name)
        )
        return f"{rendered} | {fields}" if fields else rendered


class _ConsoleFormatter(logging.Formatter):
    """为本地开发终端输出保留业务重点，避免关联字段淹没日志。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        context = get_log_context()
        action = context.get("action")
        run_id = context.get("run_id")
        suffix = ""
        if action and run_id:
            suffix = f"  [{action} · {run_id[:8]}]"
        elif action:
            suffix = f"  [{action}]"
        return f"{rendered}{suffix}"


def configure_logging(
    *,
    log_directory: str | Path,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """配置控制台、全量滚动文件和错误滚动文件。"""

    normalized_level = level.strip().upper()
    numeric_level = logging.getLevelNamesMapping().get(normalized_level)
    if not isinstance(numeric_level, int):
        raise ValueError(f"不支持的日志级别：{level}")
    if max_bytes < 1:
        raise ValueError("max_bytes 必须大于 0")
    if backup_count < 1:
        raise ValueError("backup_count 必须大于 0")

    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    file_formatter = _ContextFormatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = _ConsoleFormatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    with _CONFIGURATION_LOCK:
        logger = logging.getLogger(LOGGER_NAME)
        _close_handlers(logger)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        console = logging.StreamHandler(sys.stderr)
        console.setLevel(numeric_level)
        console.setFormatter(console_formatter)

        application_file = RotatingFileHandler(
            directory / "storyweaver.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        application_file.setLevel(numeric_level)
        application_file.setFormatter(file_formatter)

        error_file = RotatingFileHandler(
            directory / "storyweaver-error.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        error_file.setLevel(logging.ERROR)
        error_file.setFormatter(file_formatter)

        logger.addHandler(console)
        logger.addHandler(application_file)
        logger.addHandler(error_file)
        logger.info(
            "日志系统已初始化 level=%s directory=%s",
            normalized_level,
            directory,
        )
        return logger


def shutdown_logging() -> None:
    """关闭 StoryWeaver 自己创建的日志处理器。"""

    with _CONFIGURATION_LOCK:
        logger = logging.getLogger(LOGGER_NAME)
        _close_handlers(logger)
        logger.addHandler(logging.NullHandler())


def _close_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()
