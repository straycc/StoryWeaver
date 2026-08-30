"""Main Agent Capability Manifest 的单一事实源测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from storyweaver.api.capabilities import (
    CAPABILITY_MANIFEST,
    render_main_agent_manifest,
    validate_capability_parameters,
)
from storyweaver.api.action_surface import should_route_explicit_skill_reply
from storyweaver.api.main_agent import ConversationDecision


class CapabilityManifestTests(unittest.TestCase):
    def test_existing_and_new_capabilities_share_one_manifest(self) -> None:
        expected = {
            "cancel_chapter_plan",
            "write_batch",
            "creative_discussion",
            "create_novel",
            "approve_chapter_plan",
            "write_from_plan",
            "run_next_chapter_workflow",
        }
        self.assertTrue(expected.issubset(CAPABILITY_MANIFEST))
        rendered = render_main_agent_manifest()
        for capability_id in expected:
            self.assertEqual(rendered.count(f"- {capability_id}："), 1)

    def test_manifest_drives_strict_parameter_validation(self) -> None:
        value = validate_capability_parameters(
            "create_novel",
            {
                "title": "雨夜旅馆",
                "genre": "悬疑",
                "premise": "侦探进入旅馆调查旧案。",
                "protagonist": "林默",
                "central_conflict": "寻找真相与阻止调查的力量冲突。",
                "tone": "克制、压迫",
                "target_chapters": 12,
                "chapter_target_words": 2000,
            },
        )
        self.assertEqual(value["language"], "zh")
        with self.assertRaises(ValueError):
            validate_capability_parameters(
                "creative_discussion",
                {"objective": "讨论下一章", "unknown": True},
            )

    def test_explicit_skill_cannot_be_ignored_by_plain_reply(self) -> None:
        task = SimpleNamespace(applied_skills=(object(),))
        self.assertTrue(
            should_route_explicit_skill_reply(
                ConversationDecision(kind="reply", reply="普通回答"),
                task,  # type: ignore[arg-type]
            )
        )
        self.assertFalse(
            should_route_explicit_skill_reply(
                ConversationDecision(
                    kind="query",
                    action="query_chapter",
                    parameters={"include": "content"},
                ),
                task,  # type: ignore[arg-type]
            )
        )
        self.assertFalse(
            should_route_explicit_skill_reply(
                ConversationDecision(kind="reply", reply="普通回答"),
                SimpleNamespace(applied_skills=()),  # type: ignore[arg-type]
            )
        )


if __name__ == "__main__":
    unittest.main()
