"""SQLite 仓储的无外部服务烟测。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from storyweaver.novel_creation import (
    ChapterPlanProposal,
    ContextTrace,
    NovelStateReducer,
    ReviewReport,
)
from storyweaver.novel_creation.repository import ChapterRunCheckpoint
from storyweaver.novel_creation.exceptions import ProjectNotFoundError
from storyweaver.persistence import (
    Database,
    DatabaseSettings,
    DeletionConflictError,
    JobRepository,
    SQLAlchemyDeletionRepository,
    SQLAlchemyLongTermMemoryStore,
    SQLAlchemyChatSessionRepository,
    SQLAlchemyStoryProjectRepository,
)
from storyweaver.memory.long_term import (
    LongTermMemoryRecord,
    LongTermMemoryStatus,
    LongTermMemoryType,
    MemoryScopeType,
)
from storyweaver.persistence.tables import ChapterRunRow

from novel_fixtures import (
    BOOK_ID,
    create_chapter_delta,
    create_chapter_draft,
    create_chapter_plan,
    create_foundation,
    create_initial_state,
    create_metadata,
)


class PersistenceJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(DatabaseSettings(f"sqlite+pysqlite:///{Path(temporary.name) / 'test.db'}"))
        self.database.create_schema()
        self.addCleanup(self.database.dispose)

    def test_project_commit_and_rewrite_rollback(self) -> None:
        store = SQLAlchemyStoryProjectRepository(self.database)
        initial = create_initial_state()
        store.create_project(metadata=create_metadata(), foundation=create_foundation(), initial_state=initial)
        delta = create_chapter_delta()
        state = NovelStateReducer().apply(initial, delta)
        store.commit_chapter(book_id=BOOK_ID, plan=create_chapter_plan(), draft=create_chapter_draft(), delta=delta, new_state=state)

        record = store.begin_chapter_rewrite(BOOK_ID, 1)
        self.assertEqual(store.load_state(BOOK_ID), initial)
        store.rollback_chapter_rewrite(record)
        self.assertEqual(store.load_state(BOOK_ID), state)

    def test_job_events_and_active_book_constraint(self) -> None:
        jobs = JobRepository(self.database)
        first = jobs.create(job_type="prepare_chapter", book_id=BOOK_ID, payload={}, lock_scope="book_write")
        with self.assertRaises(Exception):
            jobs.create(job_type="write_batch", book_id=BOOK_ID, payload={}, lock_scope="book_write")
        jobs.start(first.job_id)
        jobs.succeed(first.job_id, result={"ok": True})
        second = jobs.create(job_type="write_batch", book_id=BOOK_ID, payload={}, lock_scope="book_write")
        self.assertEqual(second.status, "queued")
        self.assertEqual([item.event_type for item in jobs.events_after(first.job_id)], ["job_queued", "job_started", "job_succeeded"])

    def test_non_writing_job_does_not_hold_book_write_lock(self) -> None:
        jobs = JobRepository(self.database)
        chat = jobs.create(job_type="session_action", book_id=BOOK_ID, payload={"action": "chat"}, lock_scope="none")
        writing = jobs.create(job_type="prepare_chapter", book_id=BOOK_ID, payload={}, lock_scope="book_write")
        self.assertEqual(chat.lock_scope, "none")
        self.assertEqual(writing.lock_scope, "book_write")

    def test_conversation_memory_can_be_replaced_or_permanently_deleted(self) -> None:
        store = SQLAlchemyLongTermMemoryStore(self.database)
        original = self._memory_record("memory-original", "保持江湖悬疑感")
        corrected = self._memory_record(
            "memory-corrected", "近期不要揭穿师父", supersedes_id=original.memory_id
        )
        self.assertTrue(store.save(original))
        self.assertTrue(store.save(corrected))
        self.assertEqual(store.get(original.memory_id).status, LongTermMemoryStatus.SUPERSEDED)
        self.assertEqual(store.get(corrected.memory_id).status, LongTermMemoryStatus.ACTIVE)

        store.delete(corrected.memory_id)
        with self.assertRaises(KeyError):
            store.get(corrected.memory_id)

    @staticmethod
    def _memory_record(memory_id: str, content: str, *, supersedes_id: str | None = None) -> LongTermMemoryRecord:
        return LongTermMemoryRecord(
            memory_id=memory_id,
            memory_type=LongTermMemoryType.USER_PREFERENCE,
            scope_type=MemoryScopeType.BOOK,
            scope_id=BOOK_ID,
            name="创作偏好",
            description=content,
            content=content,
            importance=3,
            source_refs=("session:test",),
            fingerprint=SQLAlchemyLongTermMemoryStore.fingerprint(content),
            status=LongTermMemoryStatus.ACTIVE,
            created_at="2026-08-25T00:00:00+00:00",
            updated_at="2026-08-25T00:00:00+00:00",
            supersedes_id=supersedes_id,
        )

    def test_candidate_and_plan_query_projections_are_maintained(self) -> None:
        store = SQLAlchemyStoryProjectRepository(self.database)
        store.create_project(
            metadata=create_metadata(),
            foundation=create_foundation(),
            initial_state=create_initial_state(),
        )
        proposal = ChapterPlanProposal(
            proposal_id="proposal-1",
            book_id=BOOK_ID,
            chapter_number=1,
            base_chapter_number=0,
            base_current_time="深夜十一点",
            version=1,
            status="pending",
            plan=create_chapter_plan(),
            user_instruction=None,
            feedback_history=(),
            selected_memory_ids=(),
            selected_memory_descriptions=(),
            created_at="2026-08-23T00:00:00+00:00",
            updated_at="2026-08-23T00:00:00+00:00",
        )
        store.save_plan_proposal(proposal)
        approved = replace(
            proposal,
            status="approved",
            updated_at="2026-08-23T00:02:00+00:00",
        )
        store.save_plan_proposal(approved)
        self.assertEqual(
            store.load_plan_proposal(BOOK_ID, proposal.proposal_id).status,
            "approved",
        )
        store.save_plan_proposal(
            replace(
                approved,
                version=2,
                status="pending",
                feedback_history=("加强雨夜压迫感",),
                updated_at="2026-08-23T00:05:00+00:00",
            )
        )
        draft = create_chapter_draft()
        review = ReviewReport(passed=True, summary="测试审查通过", issues=(), score=90)
        metadata = store.save_chapter_candidate(
            proposal=proposal,
            draft=draft,
            final_draft=draft,
            initial_review=review,
            final_review=review,
            context_trace=ContextTrace(
                chapter_number=1,
                selected_source_ids=(),
                excluded_source_ids=(),
                protected_source_ids=(),
                budget=1,
                notes=(),
            ),
            reason="测试候选稿",
            revised=False,
            draft_history=(draft,),
            review_history=(review,),
        )

        with self.database.session() as session:
            run = session.get(ChapterRunRow, proposal.proposal_id)
        assert run is not None
        self.assertEqual(run.chapter_number, 1)
        self.assertEqual(run.status, "review_rejected")
        self.assertEqual(run.version, 2)
        self.assertEqual(run.updated_at, metadata.created_at)
        self.assertEqual({item["created_at"] for item in run.plan_history_json}, {
            "2026-08-23T00:00:00+00:00",
            "2026-08-23T00:05:00+00:00",
        })
        self.assertEqual(run.artifacts_json["candidate_metadata"]["proposal_id"], proposal.proposal_id)
        self.assertEqual(run.artifacts_json["candidate_metadata"]["chapter_number"], 1)
        self.assertEqual(run.artifacts_json["candidate_metadata"]["reason"], "测试候选稿")

    def test_chapter_checkpoint_round_trip_and_plan_revision_clears_it(self) -> None:
        """稳定 Worker 产物应可恢复；计划变更后必须失效。"""

        store = SQLAlchemyStoryProjectRepository(self.database)
        store.create_project(
            metadata=create_metadata(),
            foundation=create_foundation(),
            initial_state=create_initial_state(),
        )
        proposal = ChapterPlanProposal(
            proposal_id="proposal-checkpoint",
            book_id=BOOK_ID,
            chapter_number=1,
            base_chapter_number=0,
            base_current_time="深夜十一点",
            version=1,
            status="pending",
            plan=create_chapter_plan(),
            user_instruction=None,
            feedback_history=(),
            selected_memory_ids=(),
            selected_memory_descriptions=(),
            created_at="2026-08-23T00:00:00+00:00",
            updated_at="2026-08-23T00:00:00+00:00",
        )
        store.save_plan_proposal(proposal)
        draft = create_chapter_draft()
        trace = ContextTrace(
            chapter_number=1,
            selected_source_ids=("plan:1",),
            excluded_source_ids=(),
            protected_source_ids=("plan:1",),
            budget=100,
            notes=(),
        )
        store.save_chapter_checkpoint(
            proposal,
            ChapterRunCheckpoint(
                stage="draft_ready",
                draft_history=(draft,),
                review_history=(),
                previous_review=None,
                revision_count=0,
                context_trace=trace,
            ),
        )
        checkpoint = store.load_chapter_checkpoint(BOOK_ID, proposal.proposal_id)
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        self.assertEqual(checkpoint.stage, "draft_ready")
        self.assertEqual(checkpoint.draft_history, (draft,))

        store.save_plan_proposal(
            replace(
                proposal,
                version=2,
                feedback_history=("调整本章节奏",),
                updated_at="2026-08-23T00:05:00+00:00",
            )
        )
        self.assertIsNone(store.load_chapter_checkpoint(BOOK_ID, proposal.proposal_id))

    def test_session_list_projection_tracks_messages_and_binding(self) -> None:
        sessions = SQLAlchemyChatSessionRepository(self.database)
        created = sessions.create_session(title="新对话")
        sessions.append_message(created.session_id, role="user", content="帮我继续写这一章")
        sessions.bind_book(created.session_id, BOOK_ID, allow_nonempty=True)

        summary = sessions.list_sessions()[0]
        self.assertEqual(summary.session_id, created.session_id)
        self.assertEqual(summary.title, "帮我继续写这一章")
        self.assertEqual(summary.book_id, BOOK_ID)
        self.assertEqual(summary.message_count, 1)

    def test_session_deletion_rejects_active_job_then_removes_transcript(self) -> None:
        sessions = SQLAlchemyChatSessionRepository(self.database)
        jobs = JobRepository(self.database)
        deletions = SQLAlchemyDeletionRepository(self.database)
        created = sessions.create_session()
        active = jobs.create(
            job_type="session_action",
            book_id=None,
            payload={"session_id": created.session_id},
        )

        with self.assertRaises(DeletionConflictError):
            deletions.delete_session(created.session_id)
        jobs.succeed(active.job_id)
        deletions.delete_session(created.session_id)

        with self.assertRaises(KeyError):
            sessions.load_session(created.session_id)
        # 删除对话不抹掉已经完成的 Job 审计记录。
        self.assertEqual(jobs.get(active.job_id).status, "succeeded")

    def test_book_deletion_unbinds_sessions_and_removes_book_memory(self) -> None:
        projects = SQLAlchemyStoryProjectRepository(self.database)
        sessions = SQLAlchemyChatSessionRepository(self.database)
        memories = SQLAlchemyLongTermMemoryStore(self.database)
        deletions = SQLAlchemyDeletionRepository(self.database)
        projects.create_project(
            metadata=create_metadata(),
            foundation=create_foundation(),
            initial_state=create_initial_state(),
        )
        chat = sessions.create_session(book_id=BOOK_ID)
        book_memory = self._memory_record("book-memory", "只属于当前作品")
        self.assertTrue(memories.save(book_memory))

        result = deletions.delete_book(BOOK_ID)

        self.assertEqual(result.unbound_session_ids, (chat.session_id,))
        self.assertIsNone(sessions.load_session(chat.session_id).book_id)
        self.assertEqual(sessions.list_events(chat.session_id)[-1].event_type, "book_bound")
        self.assertEqual(sessions.list_events(chat.session_id)[-1].payload["book_id"], None)
        with self.assertRaises(ProjectNotFoundError):
            projects.load_project(BOOK_ID)
        with self.assertRaises(KeyError):
            memories.get(book_memory.memory_id)

    def test_book_deletion_rejects_active_job(self) -> None:
        projects = SQLAlchemyStoryProjectRepository(self.database)
        jobs = JobRepository(self.database)
        deletions = SQLAlchemyDeletionRepository(self.database)
        projects.create_project(
            metadata=create_metadata(),
            foundation=create_foundation(),
            initial_state=create_initial_state(),
        )
        jobs.create(job_type="session_action", book_id=BOOK_ID, payload={})

        with self.assertRaises(DeletionConflictError):
            deletions.delete_book(BOOK_ID)
        self.assertEqual(projects.load_metadata(BOOK_ID).book_id, BOOK_ID)


if __name__ == "__main__":
    unittest.main()
