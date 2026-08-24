"""Web 实时运行进度仓库测试。"""

from __future__ import annotations

import threading
import time
import unittest

from storyweaver.application.run_progress import RunProgressStore


class RunProgressStoreTests(unittest.TestCase):
    def test_replays_events_after_cursor_and_reports_terminal_state(self) -> None:
        store = RunProgressStore()
        store.start_run(
            "run-replay",
            session_id="session-1",
            action="confirm_chapter_plan",
            label="按计划生成章节",
        )
        store.append(
            "run-replay",
            "stage_started",
            {"agent_id": "novel-writer", "display_name": "正文写作"},
        )
        store.append(
            "run-replay",
            "stage_completed",
            {"agent_id": "novel-writer", "total_tokens": 120},
        )
        store.complete_run("run-replay", summary="章节已提交")

        events, terminal = store.wait_after(
            "run-replay",
            after_sequence=2,
            timeout=0,
        )

        self.assertTrue(terminal)
        self.assertEqual(
            [event.event_type for event in events],
            ["stage_completed", "run_completed"],
        )
        self.assertTrue(store.run_metadata("run-replay")["terminal"])

    def test_subscriber_can_wait_until_request_registers_run(self) -> None:
        store = RunProgressStore()

        def register_later() -> None:
            time.sleep(0.02)
            store.start_run(
                "run-later",
                session_id="session-2",
                action="write_next",
                label="写下一章",
            )

        thread = threading.Thread(target=register_later)
        thread.start()
        try:
            self.assertTrue(store.wait_for_run("run-later", timeout=1.0))
        finally:
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
