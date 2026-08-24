"""跨同步、异步调用链传递日志上下文。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator


_LOG_CONTEXT: ContextVar[dict[str, str]] = ContextVar(
    "storyweaver_log_context",
    default={},
)


def get_log_context() -> dict[str, str]:
    """返回当前调用链的日志上下文副本。"""

    return dict(_LOG_CONTEXT.get())


@contextmanager
def logging_context(**values: object) -> Iterator[None]:
    """在当前调用链中临时绑定结构化日志字段。"""

    context = get_log_context()
    context.update(
        {
            key: str(value)
            for key, value in values.items()
            if value is not None and str(value).strip()
        }
    )
    token = _LOG_CONTEXT.set(context)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)
