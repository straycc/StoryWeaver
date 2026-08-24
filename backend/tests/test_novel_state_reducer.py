"""小说权威状态归约测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from novel_fixtures import create_chapter_delta, create_initial_state
from storyweaver.novel_creation import (
    CharacterStateUpdate,
    FactRecord,
    HookUpdate,
    NovelStateReducer,
    StateTransitionError,
    StoryStateDelta,
)


class NovelStateReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reducer = NovelStateReducer()
        self.initial_state = create_initial_state()

    def test_apply_delta_returns_new_state_without_mutating_old_state(self) -> None:
        new_state = self.reducer.apply(self.initial_state, create_chapter_delta())

        self.assertEqual(self.initial_state.last_committed_chapter, 0)
        self.assertEqual(new_state.last_committed_chapter, 1)
        self.assertEqual(new_state.current_location, "废弃旅馆大厅")
        detective = next(
            item for item in new_state.characters if item.character_id == "lin-mo"
        )
        self.assertIn("地下室铜钥匙", detective.possessions)
        self.assertIn("register-key", detective.known_fact_ids)
        hook = next(item for item in new_state.hooks if item.hook_id == "basement-door")
        self.assertEqual(hook.status, "progressing")
        self.assertEqual(hook.last_advanced_chapter, 1)

    def test_semantically_duplicate_fact_is_not_appended(self) -> None:
        duplicate = FactRecord(
            fact_id="duplicate-hotel-status",
            subject_id=" HOTEL ",
            predicate="STATUS",
            value="  abandoned ",
            valid_from_chapter=1,
            valid_until_chapter=None,
            source_chapter=1,
            importance=2,
        )
        delta = StoryStateDelta(
            source_chapter=1,
            chapter_summary="林默再次确认旅馆已经废弃。",
            character_updates=(
                CharacterStateUpdate(
                    character_id="lin-mo",
                    learned_fact_ids=("duplicate-hotel-status",),
                ),
            ),
            new_facts=(duplicate,),
            invalidated_fact_ids=(),
            new_hooks=(),
            hook_updates=(),
        )

        new_state = self.reducer.apply(self.initial_state, delta)

        self.assertEqual(len(new_state.facts), len(self.initial_state.facts))
        detective = next(
            item for item in new_state.characters if item.character_id == "lin-mo"
        )
        self.assertIn("hotel-abandoned", detective.known_fact_ids)
        self.assertNotIn("duplicate-hotel-status", detective.known_fact_ids)

    def test_non_contiguous_chapter_is_rejected(self) -> None:
        delta = replace(create_chapter_delta(), source_chapter=2)

        with self.assertRaisesRegex(StateTransitionError, "章节号不连续"):
            self.reducer.apply(self.initial_state, delta)

    def test_resolved_hook_cannot_return_to_open(self) -> None:
        resolved_hook = replace(
            self.initial_state.hooks[0],
            status="resolved",
            last_advanced_chapter=0,
        )
        state = replace(self.initial_state, hooks=(resolved_hook,))
        delta = replace(
            create_chapter_delta(),
            hook_updates=(
                HookUpdate(
                    hook_id="basement-door",
                    status="open",
                    note="尝试重新打开伏笔",
                ),
            ),
        )

        with self.assertRaisesRegex(StateTransitionError, "不能从 resolved"):
            self.reducer.apply(state, delta)

    def test_same_possession_cannot_be_added_and_removed(self) -> None:
        delta = replace(
            create_chapter_delta(),
            character_updates=(
                CharacterStateUpdate(
                    character_id="lin-mo",
                    add_possessions=("手电筒",),
                    remove_possessions=("手电筒",),
                ),
            ),
        )

        with self.assertRaisesRegex(StateTransitionError, "同时新增和移除"):
            self.reducer.apply(self.initial_state, delta)


if __name__ == "__main__":
    unittest.main()
