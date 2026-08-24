"""无工具小说 Worker 直接使用 OpenAI Agents SDK 的回归测试。"""

from __future__ import annotations

import json
import unittest

from agents import ModelSettings
from agents.testing import ScriptedModel, assistant_message

from novel_fixtures import create_foundation, create_novel_request
from storyweaver.llm import WorkerRetryPolicy, WorkerSettings
from storyweaver.novel_creation.agents.architect import (
    ARCHITECT_SYSTEM_PROMPT,
    ArchitectAgent,
)
from storyweaver.novel_creation.serialization import to_data


def _settings(model: ScriptedModel) -> WorkerSettings:
    return WorkerSettings(
        worker_id="novel-architect",
        name="小说架构师",
        instructions=ARCHITECT_SYSTEM_PROMPT,
        model=model,
        model_settings=ModelSettings(),
        timeout_seconds=5,
    )


class _Sink:
    def __init__(self) -> None:
        self.types: list[str] = []

    async def on_event(self, event: object) -> None:
        self.types.append(getattr(getattr(event, "type"), "value"))


class SdkArchitectWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_sdk_without_legacy_model_or_runtime(self) -> None:
        foundation = create_foundation()
        model = ScriptedModel([
            [assistant_message(json.dumps(to_data(foundation), ensure_ascii=False))],
        ])
        sink = _Sink()
        architect = ArchitectAgent(
            sdk_settings=_settings(model),
            event_sinks=(sink,),
        )

        result = await architect.create(create_novel_request())

        self.assertEqual(result, foundation)
        self.assertEqual(
            sink.types,
            ["run_started", "model_completed", "run_finished"],
        )
        model.assert_complete()

    async def test_repairs_invalid_sdk_output_once_without_legacy_runtime(self) -> None:
        foundation = create_foundation()
        model = ScriptedModel([
            [assistant_message('{"premise":"缺字段"}')],
            [assistant_message(json.dumps(to_data(foundation), ensure_ascii=False))],
        ])
        architect = ArchitectAgent(
            sdk_settings=_settings(model),
            retry_policy=WorkerRetryPolicy(
                max_attempts=2,
                max_repairs=1,
                initial_delay_seconds=0,
            ),
        )

        result = await architect.create(create_novel_request())

        self.assertEqual(result, foundation)
        model.assert_complete()


if __name__ == "__main__":
    unittest.main()
