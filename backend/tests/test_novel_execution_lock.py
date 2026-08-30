"""小说项目进程内执行锁测试。"""

from __future__ import annotations

import asyncio
import unittest

from storyweaver.novel_creation import BookBusyError, BookExecutionLockManager
from storyweaver.novel_creation.application import NovelService


class BlockingWritePipeline:
    """让第一次写作停在执行阶段，以便发起并发请求。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run(
        self,
        *,
        book_id,
        user_instruction=None,
        batch_context=None,
        creative_task=None,
    ):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return object()


class BookExecutionLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_book_rejects_second_execution_without_running_pipeline(self) -> None:
        pipeline = BlockingWritePipeline()
        service = NovelService(
            store=object(),  # type: ignore[arg-type]
            create_pipeline=object(),  # type: ignore[arg-type]
            write_pipeline=pipeline,  # type: ignore[arg-type]
        )
        first = asyncio.create_task(
            service.write_next_chapter(book_id="rainy-hotel")
        )
        await pipeline.entered.wait()

        with self.assertRaisesRegex(BookBusyError, "正在执行章节写作"):
            await service.write_next_chapter(book_id="rainy-hotel")

        self.assertEqual(pipeline.calls, 1)
        pipeline.release.set()
        await first

    def test_different_books_have_independent_locks(self) -> None:
        manager = BookExecutionLockManager()

        with manager.acquire("book-a"):
            with manager.acquire("book-b"):
                self.assertTrue(manager.is_locked("book-a"))
                self.assertTrue(manager.is_locked("book-b"))


if __name__ == "__main__":
    unittest.main()
