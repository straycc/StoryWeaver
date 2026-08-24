"""只读工具参数 DTO 与公开 Schema 的一致性测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import cast

from pydantic import ValidationError

from storyweaver.novel_creation.review_tools import (
    FoundationQueryInput,
    FoundationQueryTool,
    OpenForeshadowingsInput,
    OpenForeshadowingsTool,
    ReviewSnapshot,
    build_sdk_read_tools,
)


class ReviewToolContractTests(unittest.TestCase):
    def test_foundation_dto_and_tool_schema_share_one_contract(self) -> None:
        value = FoundationQueryInput.model_validate(
            {"section": "world", "query": "雁门", "limit": 2}
        )
        schema = FoundationQueryTool._DEFINITION.input_schema

        self.assertEqual(value.section, "world")
        self.assertEqual(value.query, "雁门")
        self.assertEqual(value.limit, 2)
        self.assertEqual(set(schema["properties"]), {"section", "query", "limit"})
        self.assertFalse(schema["additionalProperties"])
        with self.assertRaises(ValidationError):
            FoundationQueryInput.model_validate({"topic": "雁门"})

    def test_open_foreshadowings_limit_is_consistent(self) -> None:
        schema = OpenForeshadowingsTool._DEFINITION.input_schema

        self.assertEqual(OpenForeshadowingsInput.model_validate({"limit": 10}).limit, 10)
        self.assertEqual(schema["properties"]["limit"]["maximum"], 10)
        with self.assertRaises(ValidationError):
            OpenForeshadowingsInput.model_validate({"limit": 11})

    def test_invalid_chapter_number_is_a_soft_miss(self) -> None:
        async def scenario() -> tuple[dict[str, object], list[tuple[object, ...]], list[dict[str, object]]]:
            completed: list[tuple[object, ...]] = []

            async def on_completed(*args: object) -> None:
                completed.append(args)

            evidence: list[dict[str, object]] = []
            snapshot = ReviewSnapshot(project=cast(object, None), chapter_summaries=())
            tools = build_sdk_read_tools(
                snapshot=snapshot,
                max_tool_calls=1,
                on_completed=on_completed,
                evidence=evidence,
            )
            tool = next(item for item in tools if item.name == "read_chapter_summary")
            first = await tool.on_invoke_tool(None, '{"chapter_number": 0}')
            second = await tool.on_invoke_tool(None, '{"chapter_number": 0}')
            return {"first": first, "second": second}, completed, evidence

        result, completed, evidence = asyncio.run(scenario())
        self.assertIn("chapter_number 必须是大于 0 的整数", result["first"])
        self.assertEqual(result["first"], result["second"])
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(completed), 2)
        self.assertFalse(bool(completed[0][-1]))
        self.assertTrue(bool(completed[1][-1]))


if __name__ == "__main__":
    unittest.main()
