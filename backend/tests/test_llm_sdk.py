"""函数式 OpenAI Agents SDK 调用入口测试。"""

from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import ModelSettings
from agents.testing import ScriptedModel, assistant_message
from pydantic import BaseModel

from storyweaver.llm import (
    LlmEvent,
    WorkerExecutionError,
    WorkerSettings,
    run_structured_worker,
)
from storyweaver.observability import ModelFailureDiagnosticWriter


class _Output(BaseModel):
    value: str


class _EventSink:
    def __init__(self) -> None:
        self.events: list[LlmEvent] = []

    async def on_event(self, event: LlmEvent) -> None:
        self.events.append(event)


class SdkWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_pydantic_output_and_projects_events(self) -> None:
        model = ScriptedModel([[assistant_message('{"value":"ok"}')]])
        sink = _EventSink()

        result = await run_structured_worker(
            settings=WorkerSettings(
                worker_id="test-worker",
                name="测试 Worker",
                instructions="只返回 JSON。",
                model=model,
                model_settings=ModelSettings(),
                timeout_seconds=5,
            ),
            prompt="开始",
            output_type=_Output,
            event_sinks=(sink,),
        )

        self.assertEqual(result.value, "ok")
        self.assertEqual(
            [event.type.value for event in sink.events],
            ["run_started", "model_completed", "run_finished"],
        )
        model.assert_complete()

    async def test_invalid_output_raises_worker_execution_error(self) -> None:
        model = ScriptedModel([[assistant_message('{"wrong":"field"}')]])

        with self.assertRaises(WorkerExecutionError):
            await run_structured_worker(
                settings=WorkerSettings(
                    worker_id="test-worker",
                    name="测试 Worker",
                    instructions="只返回 JSON。",
                    model=model,
                    model_settings=ModelSettings(),
                    timeout_seconds=5,
                ),
                prompt="开始",
                output_type=_Output,
            )

    async def test_invalid_json_writes_diagnostic_with_raw_content(self) -> None:
        model = ScriptedModel([[assistant_message('{"value": }')]])
        with TemporaryDirectory() as directory:
            with self.assertRaises(WorkerExecutionError):
                await run_structured_worker(
                    settings=WorkerSettings(
                        worker_id="test-worker",
                        name="测试 Worker",
                        instructions="只返回 JSON。",
                        model=model,
                        model_settings=ModelSettings(),
                        timeout_seconds=5,
                        diagnostic_writer=ModelFailureDiagnosticWriter(Path(directory)),
                    ),
                    prompt="开始",
                    output_type=_Output,
                )
            diagnostic_files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(diagnostic_files), 1)
            payload = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["response"]["content"], '{"value": }')

    async def test_extracts_final_json_after_model_explanation(self) -> None:
        model = ScriptedModel([[assistant_message('分析完成。\n```json\n{"value":"ok"}\n```')]])

        result = await run_structured_worker(
            settings=WorkerSettings(
                worker_id="test-worker",
                name="测试 Worker",
                instructions="只返回 JSON。",
                model=model,
                model_settings=ModelSettings(),
                timeout_seconds=5,
            ),
            prompt="开始",
            output_type=_Output,
        )

        self.assertEqual(result.value, "ok")
        model.assert_complete()


if __name__ == "__main__":
    unittest.main()
