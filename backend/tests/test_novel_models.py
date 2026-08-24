"""小说领域模型和严格序列化测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from novel_fixtures import (
    create_chapter_delta,
    create_chapter_draft,
    create_chapter_plan,
    create_foundation,
    create_initial_state,
)
from storyweaver.novel_creation.exceptions import SerializationError
from storyweaver.novel_creation import (
    ChapterContext,
    ChapterResult,
    ContextEntry,
    ContextTrace,
    ReviewIssue,
    ReviewReport,
    StoryHook,
)
from storyweaver.novel_creation.serialization import (
    decode_chapter_context,
    decode_chapter_result,
    decode_context_trace,
    decode_story_state,
    to_data,
)
class NovelModelTests(unittest.TestCase):
    def test_story_hook_display_name_is_backward_compatible(self) -> None:
        legacy_hook = StoryHook(
            hook_id="rain-mark",
            description="窗台留下的雨水指痕",
            status="open",
            importance=3,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="揭示来访者身份",
        )
        named_hook = replace(legacy_hook, name="雨水指痕")

        self.assertEqual(legacy_hook.display_name, legacy_hook.description)
        self.assertEqual(named_hook.display_name, "雨水指痕")

    def test_nested_story_state_round_trip(self) -> None:
        state = create_initial_state()

        restored = decode_story_state(to_data(state))

        self.assertEqual(restored, state)
        self.assertIsInstance(restored.characters, tuple)
        self.assertIsInstance(restored.characters[0].possessions, tuple)

    def test_unknown_persisted_field_is_rejected(self) -> None:
        data = to_data(create_initial_state())
        self.assertIsInstance(data, dict)
        data["unexpected"] = True

        with self.assertRaises(SerializationError):
            decode_story_state(data)

    def test_duplicate_character_id_is_rejected(self) -> None:
        foundation = create_foundation()

        with self.assertRaisesRegex(ValueError, "character_id"):
            replace(
                foundation,
                characters=(foundation.characters[0], foundation.characters[0]),
            )

    def test_context_and_trace_round_trip(self) -> None:
        entry = ContextEntry(
            source_id="plan:1",
            source_type="chapter_plan",
            content='{"chapter_number":1}',
            reason="本章计划",
            protected=True,
            priority=100,
        )
        context = ChapterContext(
            chapter_number=1,
            entries=(entry,),
            estimated_tokens=20,
        )
        trace = ContextTrace(
            chapter_number=1,
            selected_source_ids=("plan:1",),
            excluded_source_ids=(),
            protected_source_ids=("plan:1",),
            budget=100,
            notes=("测试",),
        )

        self.assertEqual(decode_chapter_context(to_data(context)), context)
        self.assertEqual(decode_context_trace(to_data(trace)), trace)

    def test_chapter_result_round_trip(self) -> None:
        report = ReviewReport(
            passed=False,
            summary="存在风格提醒",
            issues=(
                ReviewIssue(
                    category="style",
                    severity="warning",
                    description="环境描写略少",
                    suggestion="增加少量雨夜声音描写",
                ),
            ),
            score=75,
        )
        trace = ContextTrace(
            chapter_number=1,
            selected_source_ids=("plan:1",),
            excluded_source_ids=(),
            protected_source_ids=("plan:1",),
            budget=100,
            notes=("测试",),
        )
        result = ChapterResult(
            chapter_number=1,
            plan=create_chapter_plan(),
            draft=create_chapter_draft(),
            final_draft=create_chapter_draft(),
            initial_review=report,
            final_review=report,
            revised=False,
            state_delta=create_chapter_delta(),
            context_trace=trace,
            status="review_warning",
        )

        self.assertEqual(decode_chapter_result(to_data(result)), result)


if __name__ == "__main__":
    unittest.main()
