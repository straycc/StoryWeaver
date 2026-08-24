"""小说项目的进程内执行互斥。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .exceptions import BookBusyError


class BookExecutionLockManager:
    """按 ``book_id`` 隔离长时间运行的章节生成任务。

    Web 服务的不同请求运行在不同线程和事件循环中，因此这里使用线程锁，
    并采用非阻塞获取：重复请求会立即失败，避免等待后再次消耗模型 Token。
    """

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def acquire(self, book_id: str) -> Iterator[None]:
        if not isinstance(book_id, str) or not book_id.strip():
            raise ValueError("book_id 必须是非空字符串")
        with self._guard:
            lock = self._locks.setdefault(book_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise BookBusyError(
                f"作品 {book_id} 正在执行章节写作，请等待当前任务完成后再试"
            )
        try:
            yield
        finally:
            lock.release()

    def is_locked(self, book_id: str) -> bool:
        """仅用于状态展示和测试，不作为并发控制依据。"""

        with self._guard:
            lock = self._locks.get(book_id)
            return lock.locked() if lock is not None else False
