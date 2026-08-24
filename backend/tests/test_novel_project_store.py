"""小说项目保存、重载和事务提交测试。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from novel_fixtures import (
    BOOK_ID,
    create_chapter_delta,
    create_chapter_draft,
    create_chapter_plan,
    create_foundation,
    create_initial_state,
    create_metadata,
)
from storyweaver.novel_creation import (
    ChapterCommitError,
    ChapterPlanProposal,
    ChapterSummary,
    NovelProjectStore,
    NovelStateReducer,
    ProjectAlreadyExistsError,
    ProjectPersistenceError,
)


class NovelProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.books_directory = Path(self.temporary_directory.name) / "books"
        self.store = NovelProjectStore(self.books_directory)
        self.metadata = create_metadata()
        self.foundation = create_foundation()
        self.initial_state = create_initial_state()

    def create_project(self) -> None:
        self.store.create_project(
            metadata=self.metadata,
            foundation=self.foundation,
            initial_state=self.initial_state,
        )

    def commit_chapters(self, count: int) -> None:
        """提交只改变时间的连续章节，便于测试时间线回退。"""

        state = self.initial_state
        for chapter_number in range(1, count + 1):
            plan = replace(create_chapter_plan(), chapter_number=chapter_number)
            draft = replace(
                create_chapter_draft(),
                chapter_number=chapter_number,
                title=f"测试章节 {chapter_number}",
            )
            delta = replace(
                create_chapter_delta(),
                source_chapter=chapter_number,
                chapter_summary=f"第 {chapter_number} 章测试摘要。",
                character_updates=(),
                new_facts=(),
                hook_updates=(),
                new_time=f"第 {chapter_number} 章时间",
            )
            state = NovelStateReducer().apply(state, delta)
            self.store.commit_chapter(
                book_id=BOOK_ID,
                plan=plan,
                draft=draft,
                delta=delta,
                new_state=state,
            )

    def test_create_and_load_complete_project_with_snapshot_zero(self) -> None:
        self.create_project()

        loaded = NovelProjectStore(self.books_directory).load_project(BOOK_ID)

        self.assertEqual(loaded.metadata, self.metadata)
        self.assertEqual(loaded.foundation, self.foundation)
        self.assertEqual(loaded.state, self.initial_state)
        self.assertEqual(self.store.load_snapshot(BOOK_ID, 0), self.initial_state)
        self.assertEqual(self.store.get_next_chapter_number(BOOK_ID), 1)

    def test_duplicate_book_id_does_not_overwrite_project(self) -> None:
        self.create_project()
        original_state_text = (
            self.books_directory / BOOK_ID / "story_state.json"
        ).read_text(encoding="utf-8")

        with self.assertRaises(ProjectAlreadyExistsError):
            self.create_project()

        current_state_text = (
            self.books_directory / BOOK_ID / "story_state.json"
        ).read_text(encoding="utf-8")
        self.assertEqual(current_state_text, original_state_text)

    def test_commit_chapter_can_be_loaded_by_new_store_instance(self) -> None:
        self.create_project()
        delta = create_chapter_delta()
        new_state = NovelStateReducer().apply(self.initial_state, delta)

        chapter_metadata = self.store.commit_chapter(
            book_id=BOOK_ID,
            plan=create_chapter_plan(),
            draft=create_chapter_draft(),
            delta=delta,
            new_state=new_state,
        )
        restarted_store = NovelProjectStore(self.books_directory)

        self.assertEqual(chapter_metadata.chapter_number, 1)
        self.assertEqual(restarted_store.load_state(BOOK_ID), new_state)
        self.assertEqual(restarted_store.load_snapshot(BOOK_ID, 1), new_state)
        self.assertEqual(restarted_store.load_plan(BOOK_ID, 1), create_chapter_plan())
        self.assertEqual(restarted_store.load_delta(BOOK_ID, 1), delta)
        self.assertEqual(restarted_store.load_chapter(BOOK_ID, 1), create_chapter_draft())
        self.assertEqual(
            restarted_store.load_chapter_summaries(BOOK_ID),
            (ChapterSummary(chapter_number=1, summary=delta.chapter_summary),),
        )
        self.assertEqual(restarted_store.get_next_chapter_number(BOOK_ID), 2)

    def test_pending_chapter_plan_survives_restart_and_keeps_version_snapshot(self) -> None:
        self.create_project()
        proposal = ChapterPlanProposal(
            proposal_id="proposal-1",
            book_id=BOOK_ID,
            chapter_number=1,
            base_chapter_number=0,
            base_current_time="深夜十一点",
            version=1,
            status="pending",
            plan=create_chapter_plan(),
            user_instruction="加强雨夜压迫感",
            feedback_history=(),
            selected_memory_ids=("memory-1",),
            selected_memory_descriptions=("叙述偏好：使用克制语言",),
            created_at="2026-08-15T00:00:00+00:00",
            updated_at="2026-08-15T00:00:00+00:00",
        )

        self.store.save_plan_proposal(proposal)
        restarted = NovelProjectStore(self.books_directory)

        self.assertEqual(
            restarted.load_plan_proposal(BOOK_ID, proposal.proposal_id),
            proposal,
        )
        version_path = (
            self.books_directory
            / BOOK_ID
            / "plans"
            / "pending"
            / proposal.proposal_id
            / "v0001.json"
        )
        self.assertTrue(version_path.is_file())

    def test_invalid_delta_is_rejected_without_changing_files(self) -> None:
        self.create_project()
        state_path = self.books_directory / BOOK_ID / "story_state.json"
        original_state_text = state_path.read_text(encoding="utf-8")
        invalid_delta = replace(create_chapter_delta(), source_chapter=2)
        invalid_new_state = replace(self.initial_state, last_committed_chapter=2)

        with self.assertRaises(ChapterCommitError):
            self.store.commit_chapter(
                book_id=BOOK_ID,
                plan=create_chapter_plan(),
                draft=create_chapter_draft(),
                delta=invalid_delta,
                new_state=invalid_new_state,
            )

        self.assertEqual(state_path.read_text(encoding="utf-8"), original_state_text)
        self.assertEqual(self.store.load_chapter_index(BOOK_ID), ())

    def test_same_chapter_cannot_be_committed_twice(self) -> None:
        self.create_project()
        delta = create_chapter_delta()
        new_state = NovelStateReducer().apply(self.initial_state, delta)
        arguments = {
            "book_id": BOOK_ID,
            "plan": create_chapter_plan(),
            "draft": create_chapter_draft(),
            "delta": delta,
            "new_state": new_state,
        }
        self.store.commit_chapter(**arguments)

        with self.assertRaises(ChapterCommitError):
            self.store.commit_chapter(**arguments)

        self.assertEqual(len(self.store.load_chapter_index(BOOK_ID)), 1)

    def test_partial_file_failure_rolls_back_chapter_and_state(self) -> None:
        self.create_project()
        delta = create_chapter_delta()
        new_state = NovelStateReducer().apply(self.initial_state, delta)
        original_replace = self.store._replace_staged_file
        call_count = 0

        def fail_during_commit(source: Path, target: Path) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 6:
                raise OSError("测试注入的写入失败")
            original_replace(source, target)

        self.store._replace_staged_file = fail_during_commit

        with self.assertRaisesRegex(ChapterCommitError, "测试注入"):
            self.store.commit_chapter(
                book_id=BOOK_ID,
                plan=create_chapter_plan(),
                draft=create_chapter_draft(),
                delta=delta,
                new_state=new_state,
            )

        restarted_store = NovelProjectStore(self.books_directory)
        self.assertEqual(restarted_store.load_state(BOOK_ID), self.initial_state)
        self.assertEqual(restarted_store.load_chapter_index(BOOK_ID), ())
        self.assertEqual(restarted_store.get_next_chapter_number(BOOK_ID), 1)
        chapter_files = list((self.books_directory / BOOK_ID / "chapters").iterdir())
        self.assertEqual(chapter_files, [])

    def test_rewrite_archives_target_and_following_chapters_then_allows_new_commit(self) -> None:
        self.create_project()
        self.commit_chapters(3)

        record = self.store.begin_chapter_rewrite(BOOK_ID, 2)

        self.assertEqual(record.archived_chapter_numbers, (2, 3))
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 1)
        self.assertEqual(len(self.store.load_chapter_index(BOOK_ID)), 1)
        with self.assertRaises(ProjectPersistenceError):
            self.store.load_chapter(BOOK_ID, 2)
        archive = (
            self.books_directory
            / BOOK_ID
            / "history"
            / "rewrites"
            / record.rewrite_id
        )
        self.assertEqual(len(tuple((archive / "artifacts" / "chapters").iterdir())), 2)
        self.assertTrue((archive / "authority" / "story_state.json").is_file())
        self.assertTrue((archive / "rewrite.json").is_file())

        self.store.finalize_chapter_rewrite(record)
        self.assertFalse(
            (self.books_directory / BOOK_ID / ".rewrite-in-progress.json").exists()
        )

        current_state = self.store.load_state(BOOK_ID)
        replacement_delta = replace(
            create_chapter_delta(),
            source_chapter=2,
            chapter_summary="重新生成的第二章。",
            character_updates=(),
            new_facts=(),
            hook_updates=(),
        )
        replacement_state = NovelStateReducer().apply(
            current_state,
            replacement_delta,
        )
        self.store.commit_chapter(
            book_id=BOOK_ID,
            plan=replace(create_chapter_plan(), chapter_number=2),
            draft=replace(
                create_chapter_draft(),
                chapter_number=2,
                title="重写后的第二章",
            ),
            delta=replacement_delta,
            new_state=replacement_state,
        )
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 2)
        self.assertEqual(self.store.load_chapter(BOOK_ID, 2).title, "重写后的第二章")
        self.assertTrue(archive.is_dir())

    def test_rewrite_rollback_restores_complete_original_timeline(self) -> None:
        self.create_project()
        self.commit_chapters(3)
        original_state = self.store.load_state(BOOK_ID)
        original_index = self.store.load_chapter_index(BOOK_ID)

        record = self.store.begin_chapter_rewrite(BOOK_ID, 2)
        self.store.rollback_chapter_rewrite(record)

        self.assertEqual(self.store.load_state(BOOK_ID), original_state)
        self.assertEqual(self.store.load_chapter_index(BOOK_ID), original_index)
        self.assertEqual(self.store.load_chapter(BOOK_ID, 3).title, "测试章节 3")
        self.assertFalse(
            (self.books_directory / BOOK_ID / "history" / "rewrites" / record.rewrite_id).exists()
        )

    def test_rewrite_backfills_directories_missing_from_legacy_project(self) -> None:
        self.create_project()
        self.commit_chapters(3)
        project_directory = self.books_directory / BOOK_ID
        shutil.rmtree(project_directory / "plans" / "pending")
        shutil.rmtree(project_directory / "candidates")

        record = self.store.begin_chapter_rewrite(BOOK_ID, 2)

        self.assertTrue((project_directory / "plans" / "pending").is_dir())
        self.assertTrue((project_directory / "candidates").is_dir())
        self.assertEqual(self.store.load_state(BOOK_ID).last_committed_chapter, 1)
        self.store.finalize_chapter_rewrite(record)


if __name__ == "__main__":
    unittest.main()
