"""章节有限上下文的选择和 Trace 测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from novel_fixtures import (
    create_chapter_plan,
    create_foundation,
    create_initial_state,
    create_metadata,
)
from storyweaver.novel_creation import (
    ChapterContextBuilder,
    ChapterSummary,
    FactRecord,
    NovelProject,
    StoryHook,
)
from storyweaver.memory import (
    LongTermMemoryRecord,
    LongTermMemoryStatus,
    LongTermMemoryType,
    MemoryScopeType,
)


def create_history_project() -> NovelProject:
    initial_state = create_initial_state()
    facts = [
        FactRecord(
            fact_id="early-basement-key",
            subject_id="basement-key",
            predicate="opens",
            value="地下室钥匙能够打开地下室铁门",
            valid_from_chapter=1,
            valid_until_chapter=None,
            source_chapter=1,
            importance=5,
        ),
        FactRecord(
            fact_id="invalid-old-location",
            subject_id="stranger",
            predicate="location",
            value="旅馆大厅",
            valid_from_chapter=1,
            valid_until_chapter=3,
            source_chapter=1,
            importance=5,
        ),
    ]
    for index in range(12):
        facts.append(
            FactRecord(
                fact_id=f"ordinary-{index}",
                subject_id="hotel",
                predicate="observation",
                value=f"第 {index} 条普通走廊观察",
                valid_from_chapter=max(1, index % 5 + 1),
                valid_until_chapter=None,
                source_chapter=max(1, index % 5 + 1),
                importance=1,
            )
        )
    hooks = (
        initial_state.hooks[0],
        StoryHook(
            hook_id="missing-register-page",
            description="登记册最后一页被撕走",
            status="progressing",
            importance=5,
            opened_chapter=1,
            last_advanced_chapter=3,
            expected_payoff="揭示十年前最后一位住客",
        ),
        StoryHook(
            hook_id="resolved-window",
            description="破窗的来源",
            status="resolved",
            importance=4,
            opened_chapter=1,
            last_advanced_chapter=2,
            expected_payoff="确认破窗由暴雨造成",
        ),
        *tuple(
            StoryHook(
                hook_id=f"ordinary-hook-{index}",
                description=f"第 {index} 条普通伏笔",
                status="open",
                importance=2,
                opened_chapter=1,
                last_advanced_chapter=1,
                expected_payoff=f"回收普通伏笔 {index}",
            )
            for index in range(7)
        ),
    )
    state = replace(
        initial_state,
        last_committed_chapter=5,
        facts=tuple(facts),
        hooks=hooks,
    )
    return NovelProject(
        metadata=create_metadata(),
        foundation=create_foundation(),
        state=state,
    )


def create_next_plan():
    return replace(create_chapter_plan(), chapter_number=6)


def create_summaries() -> tuple[ChapterSummary, ...]:
    return tuple(
        ChapterSummary(chapter_number=number, summary=f"第 {number} 章摘要")
        for number in range(1, 6)
    )


class ChapterContextBuilderTests(unittest.TestCase):
    def test_long_term_directive_is_protected_but_reference_is_not_injected(self) -> None:
        common = {
            "scope_type": MemoryScopeType.BOOK,
            "scope_id": "rainy-hotel",
            "importance": 5,
            "source_refs": ("session:test",),
            "status": LongTermMemoryStatus.ACTIVE,
            "created_at": "2026-08-14T00:00:00+00:00",
            "updated_at": "2026-08-14T00:00:00+00:00",
        }
        directive = LongTermMemoryRecord(
            memory_id="directive-1",
            memory_type=LongTermMemoryType.PROJECT_DIRECTIVE,
            name="叙事视角",
            description="保持林默有限视角",
            content="不得切换到神秘人的内心视角",
            fingerprint="fingerprint-directive",
            **common,
        )
        reference = LongTermMemoryRecord(
            memory_id="reference-1",
            memory_type=LongTermMemoryType.REFERENCE,
            name="参考作品",
            description="仅供灵感参考",
            content="参考某部悬疑作品的节奏",
            fingerprint="fingerprint-reference",
            **common,
        )

        context, trace = ChapterContextBuilder(token_budget=10000).build(
            project=create_history_project(),
            plan=create_next_plan(),
            long_term_memories=(directive, reference),
        )

        self.assertIn("long-term-memory:directive-1", trace.protected_source_ids)
        self.assertNotIn("long-term-memory:reference-1", trace.selected_source_ids)
        self.assertTrue(
            any("不得切换到神秘人的内心视角" in item.content for item in context.entries)
        )

    def test_protected_sources_are_always_selected(self) -> None:
        builder = ChapterContextBuilder(
            token_budget=10,
            token_estimator=len,
        )

        context, trace = builder.build(
            project=create_history_project(),
            plan=create_next_plan(),
            chapter_summaries=create_summaries(),
            user_instruction="本章强化雨声带来的压迫感",
        )

        expected_protected = {
            "plan:6",
            "book-constraints",
            "story-foundation",
            "writing-rules",
            "state:5",
            "user-instruction:6",
            "character:lin-mo",
            "character:stranger",
            "hook:basement-door",
        }
        self.assertEqual(set(trace.protected_source_ids), expected_protected)
        self.assertTrue(expected_protected <= set(trace.selected_source_ids))
        self.assertGreater(context.estimated_tokens, trace.budget)
        self.assertTrue(any("超过预算" in note for note in trace.notes))

    def test_only_three_recent_summaries_are_candidates(self) -> None:
        builder = ChapterContextBuilder(
            token_budget=10000,
            recent_summary_limit=3,
        )

        _, trace = builder.build(
            project=create_history_project(),
            plan=create_next_plan(),
            chapter_summaries=create_summaries(),
        )

        self.assertTrue({"summary:3", "summary:4", "summary:5"} <= set(trace.selected_source_ids))
        self.assertTrue({"summary:1", "summary:2"} <= set(trace.excluded_source_ids))

    def test_fact_selection_is_bounded_and_keeps_relevant_early_fact(self) -> None:
        builder = ChapterContextBuilder(
            token_budget=10000,
            fact_limit=3,
        )

        _, trace = builder.build(
            project=create_history_project(),
            plan=create_next_plan(),
        )

        selected_facts = [
            source_id
            for source_id in trace.selected_source_ids
            if source_id.startswith("fact:")
        ]
        self.assertLessEqual(len(selected_facts), 3)
        self.assertIn("fact:early-basement-key", selected_facts)
        self.assertNotIn("fact:invalid-old-location", trace.selected_source_ids)

    def test_budget_excludes_compressible_entries_and_records_trace(self) -> None:
        builder = ChapterContextBuilder(
            token_budget=145,
            token_estimator=lambda _: 10,
            fact_limit=10,
        )

        context, trace = builder.build(
            project=create_history_project(),
            plan=create_next_plan(),
            chapter_summaries=create_summaries(),
        )

        self.assertLessEqual(context.estimated_tokens, trace.budget)
        self.assertGreater(len(trace.excluded_source_ids), 0)
        self.assertTrue(
            set(trace.selected_source_ids).isdisjoint(trace.excluded_source_ids)
        )

    def test_resolved_hook_is_not_added_to_context(self) -> None:
        _, trace = ChapterContextBuilder(token_budget=10000).build(
            project=create_history_project(),
            plan=create_next_plan(),
        )

        self.assertNotIn("hook:resolved-window", trace.selected_source_ids)
        self.assertIn("hook:resolved-window", trace.excluded_source_ids)


if __name__ == "__main__":
    unittest.main()
