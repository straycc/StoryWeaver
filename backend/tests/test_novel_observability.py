"""小说创作运行指标测试。"""

from __future__ import annotations

import unittest
from collections import deque

from storyweaver.llm import LlmEvent, LlmEventType
from storyweaver.novel_creation.observability import NovelRunObserver
from storyweaver.observability import logging_context


class NovelRunObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_progress_elapsed_time_and_usage(self) -> None:
        timestamps = deque([10.0, 12.5])
        output: list[str] = []
        observer = NovelRunObserver(
            output=output.append,
            clock=lambda: timestamps.popleft(),
        )

        await observer.on_event(
            LlmEvent(LlmEventType.RUN_STARTED, "novel-writer", {})
        )
        await observer.on_event(
            LlmEvent(
                LlmEventType.MODEL_COMPLETED,
                "novel-writer",
                {"input_tokens": 100, "output_tokens": 50},
            )
        )
        await observer.on_event(
            LlmEvent(LlmEventType.RUN_FINISHED, "novel-writer", {})
        )

        metric = observer.records[0]
        summary = observer.summarize()
        self.assertTrue(metric.succeeded)
        self.assertEqual(metric.elapsed_seconds, 2.5)
        self.assertEqual(metric.total_tokens, 150)
        self.assertEqual(summary.run_count, 1)
        self.assertEqual(summary.total_tokens, 150)
        self.assertIn("[开始] 正文写作", output[0])
        self.assertIn("[模型] 正文写作", output[1])
        self.assertIn("[完成] 正文写作", output[2])

    async def test_reports_each_model_round_without_response_content(self) -> None:
        """模型回合事件应包含诊断指标，但不暴露模型或工具内容。"""

        output: list[str] = []
        progress: list[tuple[str, str, dict[str, object]]] = []
        observer = NovelRunObserver(
            output=output.append,
            event_sink=lambda run_id, event_type, payload: progress.append(
                (run_id, event_type, dict(payload))
            ),
        )

        with logging_context(run_id="run-model-test"):
            await observer.on_event(
                LlmEvent(
                    LlmEventType.RUN_STARTED,
                    "novel-reviewer",
                    {"max_steps": 4},
                )
            )
            await observer.on_event(
                LlmEvent(
                    LlmEventType.MODEL_COMPLETED,
                    "novel-reviewer",
                    {
                        "step": 1,
                        "tool_call_count": 4,
                        "input_tokens": 1200,
                        "output_tokens": 80,
                        "elapsed_seconds": 1.25,
                        "response_kind": "tool_calls",
                        "raw_response": {"forbidden": "不会转发"},
                    },
                )
            )

        self.assertIn("第 1/4 回合", output[1])
        self.assertIn("请求工具 4 个", output[1])
        self.assertIn("输入 1,200", output[1])
        self.assertEqual(progress[1][0], "run-model-test")
        self.assertEqual(progress[1][1], "model_completed")
        self.assertEqual(progress[1][2]["response_kind"], "tool_calls")
        self.assertNotIn("raw_response", progress[1][2])

    async def test_reports_research_round_as_completed_decision(self) -> None:
        """检索回合不是无效输出，日志应明确它完成了检索决策。"""

        output: list[str] = []
        observer = NovelRunObserver(output=output.append)

        await observer.on_event(
            LlmEvent(LlmEventType.RUN_STARTED, "novel-planner", {"max_steps": 3})
        )
        await observer.on_event(
            LlmEvent(
                LlmEventType.MODEL_COMPLETED,
                "novel-planner",
                {
                    "step": 1,
                    "input_tokens": 2785,
                    "output_tokens": 3377,
                    "elapsed_seconds": 27.23,
                    "response_kind": "research",
                },
            )
        )

        self.assertIn("完成检索决策", output[1])
        self.assertNotIn("未返回有效结果", output[1])

    async def test_failed_run_is_recorded(self) -> None:
        timestamps = deque([20.0, 21.0])
        observer = NovelRunObserver(
            output=None,
            clock=lambda: timestamps.popleft(),
        )

        await observer.on_event(
            LlmEvent(LlmEventType.RUN_STARTED, "novel-planner", {})
        )
        await observer.on_event(
            LlmEvent(
                LlmEventType.RUN_FAILED,
                "novel-planner",
                {"error": "模型不可用"},
            )
        )

        self.assertFalse(observer.records[0].succeeded)
        self.assertEqual(observer.records[0].error, "模型不可用")

    async def test_routes_stage_progress_by_context_run_id(self) -> None:
        progress: list[tuple[str, str, dict[str, object]]] = []
        observer = NovelRunObserver(
            output=None,
            clock=lambda: 10.0,
            event_sink=lambda run_id, event_type, payload: progress.append(
                (run_id, event_type, dict(payload))
            ),
        )

        with logging_context(run_id="run-observer-test"):
            await observer.on_event(
                LlmEvent(LlmEventType.RUN_STARTED, "novel-writer", {})
            )
            await observer.on_event(
                LlmEvent(LlmEventType.RUN_FINISHED, "novel-writer", {})
            )

        self.assertEqual(
            [item[1] for item in progress],
            ["stage_started", "stage_completed"],
        )
        self.assertEqual({item[0] for item in progress}, {"run-observer-test"})
        self.assertEqual(progress[0][2]["display_name"], "正文写作")

    async def test_reports_tool_completion_without_tool_output(self) -> None:
        output: list[str] = []
        progress: list[tuple[str, str, dict[str, object]]] = []
        observer = NovelRunObserver(
            output=output.append,
            event_sink=lambda run_id, event_type, payload: progress.append(
                (run_id, event_type, dict(payload))
            ),
        )

        with logging_context(run_id="run-tool-test"):
            await observer.on_event(
                LlmEvent(
                    LlmEventType.TOOL_COMPLETED,
                    "novel-reviewer",
                    {
                        "tool_name": "get_entity_evidence",
                        "succeeded": True,
                        "elapsed_seconds": 0.125,
                        "tool_call_index": 1,
                        "tool_call_limit": 4,
                        "output": {"forbidden": "不会转发"},
                    },
                )
            )

        self.assertIn("[工具完成] 章节审查 · get_entity_evidence · 1/4", output[0])
        self.assertEqual(progress[0][0], "run-tool-test")
        self.assertEqual(progress[0][1], "tool_completed")
        self.assertEqual(progress[0][2]["tool_name"], "get_entity_evidence")
        self.assertNotIn("output", progress[0][2])

    async def test_reports_tool_budget_exhaustion_as_non_error_progress(self) -> None:
        output: list[str] = []
        observer = NovelRunObserver(output=output.append)

        await observer.on_event(
            LlmEvent(
                LlmEventType.TOOL_COMPLETED,
                "novel-reviewer",
                {
                    "tool_name": "search_canon_evidence",
                    "succeeded": False,
                    "budget_exhausted": True,
                    "tool_call_index": 10,
                    "tool_call_limit": 10,
                },
            )
        )

        self.assertIn("[工具预算耗尽] 章节审查 · search_canon_evidence · 10/10", output[0])


if __name__ == "__main__":
    unittest.main()
