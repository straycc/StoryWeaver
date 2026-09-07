"""Research、EvidencePackage 与 Report 隔离边界测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from agents import ModelSettings
from pydantic import BaseModel

from storyweaver.llm import WorkerSettings
from storyweaver.llm.two_phase import _report_model_settings, run_research_then_submit


class _Output(BaseModel):
    value: str


class TwoPhaseContextTests(unittest.IsolatedAsyncioTestCase):
    def test_report_uses_low_reasoning_without_mutating_worker_settings(self) -> None:
        original = ModelSettings(
            max_tokens=12_000,
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "provider_option": True,
            },
        )

        report = _report_model_settings(original, is_repair=False)

        self.assertEqual(report.max_tokens, 12_000)
        self.assertEqual(report.extra_body["reasoning_effort"], "low")
        self.assertEqual(report.extra_body["thinking"], {"type": "enabled"})
        self.assertTrue(report.extra_body["provider_option"])
        self.assertEqual(original.extra_body["reasoning_effort"], "high")

    def test_report_repair_disables_thinking(self) -> None:
        original = ModelSettings(
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "provider_option": True,
            },
        )

        repair = _report_model_settings(original, is_repair=True)

        self.assertEqual(repair.extra_body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", repair.extra_body)
        self.assertTrue(repair.extra_body["provider_option"])

    async def test_orchestrator_runs_research_once_then_passes_compiled_package(self) -> None:
        settings = WorkerSettings(
            worker_id="test",
            name="测试",
            instructions="测试",
            model=object(),
            model_settings=ModelSettings(),
            timeout_seconds=1,
        )
        expected = _Output(value="ok")
        evidence = [{
            "tool_name": "query",
            "arguments": {"query": "x"},
            "result": {"matched": True},
            "succeeded": True,
        }]
        research = AsyncMock(return_value=None)
        report = AsyncMock(return_value=expected)

        with (
            patch("storyweaver.llm.two_phase.run_research", research),
            patch("storyweaver.llm.two_phase.run_report", report),
        ):
            result = await run_research_then_submit(
                settings=settings,
                prompt="任务",
                read_tools=[],
                output_type=_Output,
                submit_tool_name="submit",
                submit_description="提交",
                max_research_turns=2,
                evidence=evidence,
                evidence_token_budget=200,
            )

        self.assertEqual(result, expected)
        research.assert_awaited_once()
        report.assert_awaited_once()
        package = report.await_args.kwargs["evidence_package"]
        self.assertEqual(package.source_count, 1)
        self.assertEqual(package.evidence[0]["tool_name"], "query")


if __name__ == "__main__":
    unittest.main()
