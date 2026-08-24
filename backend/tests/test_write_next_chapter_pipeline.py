"""完整写下一章 Pipeline 的编排、恢复和失败原子性测试。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from novel_fixtures import (
    BOOK_ID,
    create_chapter_plan,
    create_foundation,
    create_initial_state,
    create_metadata,
)
from storyweaver.novel_creation import (
    ChapterContextBuilder,
    ChapterDraft,
    ChapterPipelineError,
    CharacterStateUpdate,
    NovelProjectStore,
    ReviewIssue,
    ReviewReport,
    StoryStateDelta,
    WriteNextChapterPipeline,
)
from storyweaver.novel_creation.application import NovelService


PASSING_REVIEW = ReviewReport(
    passed=True,
    summary="章节符合计划和连续性要求",
    issues=(),
    score=90,
)
CRITICAL_REVIEW = ReviewReport(
    passed=False,
    summary="章节存在严重连续性问题",
    issues=(
        ReviewIssue(
            category="knowledge_boundary",
            severity="critical",
            description="角色提前知道未知事实",
            suggestion="删除越界知识",
            related_source_ids=("character:lin-mo",),
        ),
    ),
    score=50,
)
WORLD_CRITICAL_REVIEW = ReviewReport(
    passed=False,
    summary="章节存在严重空间连续性问题",
    issues=(
        ReviewIssue(
            category="world_continuity",
            severity="critical",
            description="人物动作与关键物证位置冲突",
            suggestion="修正人物动作和物证位置",
            related_source_ids=("plan:1",),
        ),
    ),
    score=60,
)
WARNING_REVIEW = ReviewReport(
    passed=True,
    summary="章节存在需要修订的连续性问题",
    issues=(
        ReviewIssue(
            category="world_continuity",
            severity="warning",
            description="人物靠在墙上，但刻痕出现在炉门上",
            suggestion="统一人物动作和刻痕位置",
            related_source_ids=("plan:1",),
        ),
    ),
    score=82,
)
PARSE_FAILED_REVIEW = ReviewReport(
    passed=False,
    summary="Reviewer 输出解析失败",
    issues=(),
    parse_failed=True,
)


class FakePlanner:
    def __init__(self) -> None:
        self.calls = 0
        self.instructions = []

    async def plan(self, *, project, user_instruction=None):
        self.calls += 1
        self.instructions.append(user_instruction)
        chapter_number = project.state.last_committed_chapter + 1
        return replace(
            create_chapter_plan(),
            chapter_number=chapter_number,
            goal=f"推进第 {chapter_number} 章调查",
        )


class FakeWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def write(self, context):
        self.calls += 1
        return ChapterDraft(
            chapter_number=context.chapter_number,
            title=f"雨夜线索·{context.chapter_number}",
            content="雨" * 1080,
            word_count=1080,
        )


class FakeReviewer:
    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0
        self.full_review_calls = 0
        self.verification_calls = 0

    async def review(self, *, context, draft):
        self.calls += 1
        self.full_review_calls += 1
        if not self.reports:
            return PASSING_REVIEW
        return self.reports.pop(0)

    async def verify_revision(self, *, context, draft, original_review):
        self.calls += 1
        self.verification_calls += 1
        if not self.reports:
            return PASSING_REVIEW
        return self.reports.pop(0)


class FakeReviser:
    def __init__(self) -> None:
        self.calls = 0

    async def revise(self, *, context, draft, review):
        self.calls += 1
        return replace(
            draft,
            title=f"{draft.title}（修订）",
            content="风" * 1080,
            word_count=1080,
        )


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, *, project, plan, draft):
        self.calls += 1
        chapter_number = draft.chapter_number
        return StoryStateDelta(
            source_chapter=chapter_number,
            chapter_summary=f"第 {chapter_number} 章完成一次调查推进。",
            character_updates=(),
            new_facts=(),
            invalidated_fact_ids=(),
            new_hooks=(),
            hook_updates=(),
            new_time=f"第 {chapter_number} 章结束",
            new_location=plan.location,
        )


class InvalidAnalyzer:
    async def analyze(self, *, project, plan, draft):
        return StoryStateDelta(
            source_chapter=draft.chapter_number,
            chapter_summary="包含非法角色更新",
            character_updates=(
                CharacterStateUpdate(
                    character_id="missing-character",
                    emotion="紧张",
                ),
            ),
            new_facts=(),
            invalidated_fact_ids=(),
            new_hooks=(),
            hook_updates=(),
        )


class WriteNextChapterPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.books_directory = Path(self.temporary_directory.name) / "books"
        self.store = NovelProjectStore(self.books_directory)
        self.store.create_project(
            metadata=create_metadata(),
            foundation=create_foundation(),
            initial_state=create_initial_state(),
        )

    def create_pipeline(
        self,
        *,
        reviewer,
        reviser=None,
        analyzer=None,
        store=None,
        planner=None,
        writer=None,
        review_policy="strict",
    ):
        return WriteNextChapterPipeline(
            store=store or self.store,
            planner=planner or FakePlanner(),
            context_builder=ChapterContextBuilder(token_budget=6000),
            writer=writer or FakeWriter(),
            reviewer=reviewer,
            reviser=reviser or FakeReviser(),
            analyzer=analyzer or FakeAnalyzer(),
            review_policy=review_policy,
        )

    async def test_prepare_revise_and_confirm_are_separate_stages(self) -> None:
        planner = FakePlanner()
        writer = FakeWriter()
        pipeline = self.create_pipeline(
            reviewer=FakeReviewer([PASSING_REVIEW]),
            planner=planner,
            writer=writer,
        )

        proposal = await pipeline.prepare(
            book_id=BOOK_ID,
            user_instruction="加强雨夜压迫感",
        )

        self.assertEqual(proposal.status, "pending")
        self.assertEqual(proposal.version, 1)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(writer.calls, 0)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 0)

        revised = await pipeline.revise(
            proposal,
            feedback="神秘人不要直接出场",
        )

        self.assertEqual(revised.version, 2)
        self.assertEqual(revised.feedback_history, ("神秘人不要直接出场",))
        self.assertIn("当前候选计划", planner.instructions[-1])
        self.assertEqual(writer.calls, 0)

        result = await pipeline.execute(revised)

        self.assertEqual(result.chapter_number, 1)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 1)

    async def test_prepared_plan_expires_when_project_state_advances(self) -> None:
        pipeline = self.create_pipeline(reviewer=FakeReviewer([PASSING_REVIEW]))
        proposal = await pipeline.prepare(book_id=BOOK_ID)
        await self.create_pipeline(
            reviewer=FakeReviewer([PASSING_REVIEW])
        ).run(book_id=BOOK_ID)

        with self.assertRaisesRegex(ChapterPipelineError, "已过期"):
            await pipeline.execute(proposal)

    async def test_passing_review_skips_reviser_and_commits_all_artifacts(self) -> None:
        reviewer = FakeReviewer([PASSING_REVIEW])
        reviser = FakeReviser()
        pipeline = self.create_pipeline(reviewer=reviewer, reviser=reviser)

        result = await pipeline.run(book_id=BOOK_ID)

        self.assertFalse(result.revised)
        self.assertEqual(result.status, "ready_for_review")
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(reviser.calls, 0)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 1)
        self.assertEqual(self.store.load_review(BOOK_ID, 1), PASSING_REVIEW)
        self.assertEqual(self.store.load_review(BOOK_ID, 1, final=True), PASSING_REVIEW)
        self.assertEqual(
            self.store.load_context_trace(BOOK_ID, 1),
            result.context_trace,
        )

    async def test_critical_review_revises_once_and_saves_both_reviews(self) -> None:
        reviewer = FakeReviewer([CRITICAL_REVIEW, PASSING_REVIEW])
        reviser = FakeReviser()
        pipeline = self.create_pipeline(reviewer=reviewer, reviser=reviser)

        result = await pipeline.run(book_id=BOOK_ID)

        self.assertTrue(result.revised)
        self.assertEqual(reviser.calls, 1)
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(reviewer.full_review_calls, 1)
        self.assertEqual(reviewer.verification_calls, 1)
        self.assertEqual(result.final_review, PASSING_REVIEW)
        self.assertIn("修订", self.store.load_chapter(BOOK_ID, 1).title)
        review_directory = self.books_directory / BOOK_ID / "reviews"
        self.assertTrue((review_directory / "0001-original.md").is_file())
        self.assertTrue((review_directory / "0001-revised.md").is_file())
        self.assertTrue((review_directory / "0001-draft-r00.md").is_file())
        self.assertTrue((review_directory / "0001-draft-r01.md").is_file())
        self.assertTrue((review_directory / "0001-review-r00.json").is_file())
        self.assertTrue((review_directory / "0001-review-r01.json").is_file())
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(len(result.draft_history), 2)
        self.assertEqual(len(result.review_history), 2)
        self.assertEqual(self.store.load_review(BOOK_ID, 1), CRITICAL_REVIEW)
        self.assertEqual(self.store.load_review(BOOK_ID, 1, final=True), PASSING_REVIEW)

    async def test_blocking_warning_triggers_one_targeted_verification(self) -> None:
        reviewer = FakeReviewer([WARNING_REVIEW, PASSING_REVIEW])
        reviser = FakeReviser()

        result = await self.create_pipeline(
            reviewer=reviewer,
            reviser=reviser,
        ).run(book_id=BOOK_ID)

        self.assertTrue(result.committed)
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(reviser.calls, 1)
        self.assertEqual(reviewer.calls, 2)

    async def test_second_blocking_issue_is_saved_after_single_revision(self) -> None:
        reviewer = FakeReviewer(
            [CRITICAL_REVIEW, WORLD_CRITICAL_REVIEW, PASSING_REVIEW]
        )
        reviser = FakeReviser()

        result = await self.create_pipeline(
            reviewer=reviewer,
            reviser=reviser,
        ).run(book_id=BOOK_ID)

        self.assertFalse(result.committed)
        self.assertEqual(result.status, "draft_rejected")
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(reviser.calls, 1)
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(len(result.draft_history), 2)
        self.assertEqual(len(result.review_history), 2)

    async def test_remaining_critical_issue_is_saved_as_candidate_without_commit(self) -> None:
        reviewer = FakeReviewer([CRITICAL_REVIEW, CRITICAL_REVIEW])
        reviser = FakeReviser()
        analyzer = FakeAnalyzer()

        result = await self.create_pipeline(
            reviewer=reviewer,
            reviser=reviser,
            analyzer=analyzer,
        ).run(book_id=BOOK_ID)

        self.assertEqual(reviser.calls, 1)
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(result.status, "draft_rejected")
        self.assertFalse(result.committed)
        self.assertIsNone(result.state_delta)
        self.assertEqual(analyzer.calls, 0)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 0)
        self.assertEqual(self.store.load_chapter_index(BOOK_ID), ())
        candidate = self.store.load_chapter_candidate(
            BOOK_ID,
            result.candidate_id or "",
        )
        self.assertIn("critical", candidate.reason)
        self.assertEqual(
            self.store.load_candidate_draft(BOOK_ID, candidate.candidate_id),
            result.final_draft,
        )
        self.assertEqual(
            self.store.load_candidate_draft(
                BOOK_ID,
                candidate.candidate_id,
                final=False,
            ),
            result.draft,
        )

    async def test_strict_rejection_keeps_saved_plan_pending_for_adjustment(self) -> None:
        pipeline = self.create_pipeline(
            reviewer=FakeReviewer([CRITICAL_REVIEW, CRITICAL_REVIEW]),
        )
        proposal = await pipeline.prepare(book_id=BOOK_ID)
        self.store.save_plan_proposal(proposal)
        service = NovelService(
            store=self.store,
            create_pipeline=object(),  # type: ignore[arg-type]
            write_pipeline=pipeline,
        )

        result = await service.confirm_chapter_plan(
            book_id=BOOK_ID,
            proposal_id=proposal.proposal_id,
        )

        self.assertFalse(result.committed)
        self.assertEqual(
            self.store.load_plan_proposal(BOOK_ID, proposal.proposal_id).status,
            "pending",
        )

    async def test_parse_failed_review_saves_candidate_without_analyzing(self) -> None:
        reviewer = FakeReviewer([PARSE_FAILED_REVIEW])
        reviser = FakeReviser()
        analyzer = FakeAnalyzer()

        result = await self.create_pipeline(
            reviewer=reviewer,
            reviser=reviser,
            analyzer=analyzer,
        ).run(book_id=BOOK_ID)

        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(reviser.calls, 0)
        self.assertEqual(result.status, "draft_rejected")
        self.assertFalse(result.committed)
        self.assertTrue(result.final_review.parse_failed)
        self.assertEqual(analyzer.calls, 0)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 0)
        candidate = self.store.load_chapter_candidate(
            BOOK_ID,
            result.candidate_id or "",
        )
        self.assertIn("解析失败", candidate.reason)

    async def test_auto_policy_keeps_previous_warning_commit_behavior(self) -> None:
        result = await self.create_pipeline(
            reviewer=FakeReviewer([PARSE_FAILED_REVIEW]),
            review_policy="auto",
        ).run(book_id=BOOK_ID)

        self.assertTrue(result.committed)
        self.assertEqual(result.status, "review_warning")
        self.assertIsNotNone(result.state_delta)
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 1)

    async def test_invalid_analyzer_delta_leaves_project_unchanged(self) -> None:
        pipeline = self.create_pipeline(
            reviewer=FakeReviewer([PASSING_REVIEW]),
            analyzer=InvalidAnalyzer(),
        )

        with self.assertRaisesRegex(ValueError, "不存在的角色"):
            await pipeline.run(book_id=BOOK_ID)

        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 0)
        self.assertEqual(self.store.load_chapter_index(BOOK_ID), ())
        self.assertEqual(
            list((self.books_directory / BOOK_ID / "chapters").iterdir()),
            [],
        )

    async def test_three_chapters_then_restart_and_write_fourth(self) -> None:
        pipeline = self.create_pipeline(reviewer=FakeReviewer([]))
        for expected_chapter in range(1, 4):
            result = await pipeline.run(book_id=BOOK_ID)
            self.assertEqual(result.chapter_number, expected_chapter)

        restarted_store = NovelProjectStore(self.books_directory)
        restarted_pipeline = self.create_pipeline(
            reviewer=FakeReviewer([]),
            store=restarted_store,
        )
        fourth = await restarted_pipeline.run(book_id=BOOK_ID)

        self.assertEqual(fourth.chapter_number, 4)
        self.assertEqual(restarted_store.get_next_chapter_number(BOOK_ID), 5)
        self.assertEqual(len(restarted_store.load_chapter_summaries(BOOK_ID)), 4)
        self.assertEqual(len(restarted_store.load_chapter_index(BOOK_ID)), 4)


if __name__ == "__main__":
    unittest.main()
