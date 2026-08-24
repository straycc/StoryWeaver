"""伏笔治理 V1 的确定性测试。"""

from __future__ import annotations

from dataclasses import replace
import unittest

from novel_fixtures import create_chapter_delta, create_initial_state, create_metadata, create_foundation
from storyweaver.novel_creation import (
    HookManager,
    HookPlan,
    NovelProject,
    StoryHook,
)
from novel_fixtures import create_chapter_plan


class HookManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = NovelProject(
            metadata=create_metadata(),
            foundation=create_foundation(),
            state=create_initial_state(),
        )
        self.manager = HookManager()

    def test_merges_a_clearly_duplicate_new_hook_into_existing_hook(self) -> None:
        candidate = StoryHook(
            hook_id="basement-door-again",
            name="地下室铁门再次被锁住",
            description="旅馆地下室铁门被人为锁住，林默再次发现新的锁痕。",
            status="progressing",
            importance=4,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="揭开地下室门后的秘密",
        )
        delta = replace(create_chapter_delta(), new_hooks=(candidate,), hook_updates=())

        reconciled = self.manager.reconcile(project=self.project, delta=delta)

        self.assertEqual(reconciled.new_hooks, ())
        self.assertEqual(reconciled.hook_updates[0].hook_id, "basement-door")
        self.assertEqual(reconciled.hook_updates[0].status, "progressing")

    def test_keeps_only_one_unmatched_new_hook_per_chapter(self) -> None:
        first = StoryHook(
            hook_id="new-first",
            name="陌生铜哨",
            description="林默在柜台下发现一枚陌生铜哨。",
            status="open",
            importance=2,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="说明铜哨来源",
        )
        second = replace(first, hook_id="new-second", name="窗外黑伞", description="旅馆窗外出现一把从不移动的黑伞。")

        reconciled = self.manager.reconcile(
            project=self.project,
            delta=replace(create_chapter_delta(), new_hooks=(first, second)),
        )

        self.assertEqual(tuple(hook.hook_id for hook in reconciled.new_hooks), ("new-first",))

    def test_report_records_merge_and_budget_drop(self) -> None:
        duplicate = StoryHook(
            hook_id="basement-door-copy",
            name="地下室铁门再次被锁住",
            description="旅馆地下室铁门被人为锁住，锁痕更新。",
            status="progressing",
            importance=4,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="揭开地下室秘密",
        )
        first = StoryHook(
            hook_id="new-first",
            name="陌生铜哨",
            description="林默在柜台下发现一枚陌生铜哨。",
            status="open",
            importance=2,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="说明铜哨来源",
        )
        second = replace(first, hook_id="new-second", name="窗外黑伞", description="旅馆窗外出现一把黑伞。")

        _, report = self.manager.reconcile_with_report(
            project=self.project,
            delta=replace(create_chapter_delta(), new_hooks=(duplicate, first, second), hook_updates=()),
        )

        self.assertEqual(report.merged_pairs, (("basement-door-copy", "basement-door"),))
        self.assertEqual(report.created_hook_ids, ("new-first",))
        self.assertEqual(report.dropped_hook_ids, ("new-second",))

    def test_planning_guidance_prioritizes_long_unadvanced_hook(self) -> None:
        guidance = self.manager.planning_guidance(project=self.project)

        self.assertEqual(guidance["new_hook_budget"], 1)
        self.assertTrue(guidance["priority_hooks"])

    def test_final_chapter_forbids_new_hook_and_reports_missed_resolution(self) -> None:
        project = replace(
            self.project,
            metadata=replace(self.project.metadata, target_chapters=1),
        )
        new_hook = StoryHook(
            hook_id="ending-new-hook",
            name="不该新增的谜团",
            description="结局突然出现的新谜团。",
            status="open",
            importance=2,
            opened_chapter=1,
            last_advanced_chapter=1,
            expected_payoff="不会在本书回收",
        )
        plan = replace(
            create_chapter_plan(),
            hook_plan=HookPlan(
                resolve_hook_ids=("basement-door",),
                new_hook_budget=0,
            ),
        )

        _, report = self.manager.reconcile_with_report(
            project=project,
            plan=plan,
            delta=replace(create_chapter_delta(), new_hooks=(new_hook,)),
        )

        self.assertEqual(report.dropped_hook_ids, ("ending-new-hook",))
        self.assertEqual(report.missed_resolution_ids, ("basement-door",))
        self.assertEqual(report.ending_risk_hook_ids, ("basement-door",))


if __name__ == "__main__":
    unittest.main()
