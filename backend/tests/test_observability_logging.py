"""Logging V1 的无网络测试。"""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from storyweaver.observability import (
    configure_logging,
    logging_context,
    shutdown_logging,
)


class LoggingConfigurationTests(unittest.TestCase):
    def test_console_context_traceback_and_file_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "logs"
            console = io.StringIO()
            with redirect_stderr(console):
                configure_logging(
                    log_directory=directory,
                    level="INFO",
                    max_bytes=700,
                    backup_count=2,
                )
                logger = logging.getLogger("storyweaver.tests.logging")
                try:
                    with logging_context(
                        request_id="request-test",
                        session_id="session-test",
                        agent_id="chapter-analyzer",
                    ):
                        logger.info("章节分析开始")
                        try:
                            raise RuntimeError("模拟分析失败")
                        except RuntimeError:
                            logger.exception("章节分析异常")
                        for index in range(30):
                            logger.info(
                                "轮转测试 index=%d payload=%s",
                                index,
                                "x" * 80,
                            )
                finally:
                    shutdown_logging()

            log_files = tuple(directory.glob("storyweaver.log*"))
            self.assertGreaterEqual(len(log_files), 2)
            all_log_text = "\n".join(
                path.read_text(encoding="utf-8") for path in log_files
            )
            error_text = (directory / "storyweaver-error.log").read_text(
                encoding="utf-8"
            )
            self.assertIn("request_id=request-test", all_log_text)
            self.assertIn("agent_id=chapter-analyzer", all_log_text)
            self.assertIn("RuntimeError: 模拟分析失败", error_text)
            self.assertIn("章节分析异常", console.getvalue())
            self.assertIn("INFO", console.getvalue())
            self.assertNotIn("request_id=request-test", console.getvalue())


if __name__ == "__main__":
    unittest.main()
