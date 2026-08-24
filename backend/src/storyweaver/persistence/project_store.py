"""PostgreSQL 版小说项目仓储。

它与 ``NovelProjectStore`` 保持相同的领域方法，因而 Pipeline、Reducer 和
HookManager 无需了解数据库细节。章节与快照不可覆盖，提交时锁住作品行。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ..novel_creation.exceptions import (
    ChapterCommitError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ProjectPersistenceError,
)
from ..novel_creation.models import (
    BookMetadata,
    ChapterCandidateMetadata,
    ChapterDraft,
    ChapterMetadata,
    ChapterPlan,
    ChapterPlanProposal,
    ChapterRewriteRecord,
    ChapterSummary,
    ContextTrace,
    NovelFoundation,
    NovelProject,
    ReviewReport,
    StoryState,
    StoryStateDelta,
)
from ..novel_creation.serialization import (
    decode_book_metadata,
    decode_chapter_candidate_metadata,
    decode_chapter_draft,
    decode_chapter_metadata,
    decode_chapter_plan,
    decode_chapter_plan_proposal,
    decode_chapter_rewrite_record,
    decode_chapter_summary,
    decode_context_trace,
    decode_novel_foundation,
    decode_review_report,
    decode_story_state,
    decode_story_state_delta,
    to_data,
)
from ..novel_creation.state_reducer import NovelStateReducer
from .database import Database
from .tables import (
    BookRow,
    ChapterCandidateRow,
    ChapterRow,
    PlanProposalRow,
    PlanProposalVersionRow,
    RewriteRow,
    StorySnapshotRow,
)


def _data(value: object) -> dict[str, Any]:
    encoded = to_data(value)
    if not isinstance(encoded, dict):  # pragma: no cover - 领域对象必须编码为对象
        raise TypeError("持久化领域对象必须编码为 JSON 对象")
    return encoded


def _data_list(values: tuple[object, ...]) -> list[object]:
    return [to_data(value) for value in values]


class PostgresNovelProjectStore:
    """以 PostgreSQL 为唯一事实源的小说仓储。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._reducer = NovelStateReducer()

    def create_project(
        self,
        *,
        metadata: BookMetadata,
        foundation: NovelFoundation,
        initial_state: StoryState,
    ) -> NovelProject:
        self._validate_initial_project(metadata, foundation, initial_state)
        with self.database.session() as session:
            try:
                with session.begin():
                    session.add(BookRow(
                        book_id=metadata.book_id,
                        metadata_json=_data(metadata),
                        foundation_json=_data(foundation),
                        state_json=_data(initial_state),
                        version=0,
                    ))
                    session.add(StorySnapshotRow(
                        book_id=metadata.book_id,
                        chapter_number=0,
                        state_json=_data(initial_state),
                    ))
            except IntegrityError as exc:
                raise ProjectAlreadyExistsError(f"项目已存在：{metadata.book_id}") from exc
        return NovelProject(metadata=metadata, foundation=foundation, state=initial_state)

    def load_project(self, book_id: str) -> NovelProject:
        row = self._book(book_id)
        return NovelProject(
            metadata=decode_book_metadata(row.metadata_json),
            foundation=decode_novel_foundation(row.foundation_json),
            state=decode_story_state(row.state_json),
        )

    def load_project_with_version(self, book_id: str) -> tuple[NovelProject, int]:
        """读取作品与数据库事务版本，供 Context Snapshot 固定基线。"""

        row = self._book(book_id)
        return (
            NovelProject(
                metadata=decode_book_metadata(row.metadata_json),
                foundation=decode_novel_foundation(row.foundation_json),
                state=decode_story_state(row.state_json),
            ),
            int(row.version),
        )

    def list_projects(self) -> tuple[BookMetadata, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(BookRow).order_by(BookRow.book_id)).all()
        return tuple(decode_book_metadata(row.metadata_json) for row in rows)

    def load_metadata(self, book_id: str) -> BookMetadata:
        return decode_book_metadata(self._book(book_id).metadata_json)

    def load_foundation(self, book_id: str) -> NovelFoundation:
        return decode_novel_foundation(self._book(book_id).foundation_json)

    def load_state(self, book_id: str) -> StoryState:
        return decode_story_state(self._book(book_id).state_json)

    def load_snapshot(self, book_id: str, chapter_number: int) -> StoryState:
        with self.database.session() as session:
            row = session.scalar(select(StorySnapshotRow).where(
                StorySnapshotRow.book_id == book_id,
                StorySnapshotRow.chapter_number == chapter_number,
            ))
        if row is None:
            raise ProjectPersistenceError(f"项目 {book_id} 不存在第 {chapter_number} 章快照")
        return decode_story_state(row.state_json)

    def load_chapter_index(self, book_id: str) -> tuple[ChapterMetadata, ...]:
        self._book(book_id)
        with self.database.session() as session:
            rows = session.scalars(select(ChapterRow).where(
                ChapterRow.book_id == book_id,
            ).order_by(ChapterRow.chapter_number)).all()
        return tuple(decode_chapter_metadata(row.metadata_json) for row in rows)

    def get_next_chapter_number(self, book_id: str) -> int:
        return self.load_state(book_id).last_committed_chapter + 1

    def load_chapter_summaries(self, book_id: str) -> tuple[ChapterSummary, ...]:
        rows = self._chapter_rows(book_id)
        # 文件仓储返回 ChapterSummary 对象；数据库仓储也必须保持同一领域
        # 契约，不能只泄漏摘要字符串给 Planner/Reviewer 的只读工具。
        summaries = tuple(
            ChapterSummary(
                chapter_number=row.chapter_number,
                summary=decode_story_state_delta(row.delta_json).chapter_summary,
            )
            for row in rows
        )
        state = self.load_state(book_id)
        if len(summaries) != state.last_committed_chapter:
            raise ProjectPersistenceError("章节摘要数量与当前状态不一致")
        return summaries

    def load_chapter(self, book_id: str, chapter_number: int) -> ChapterDraft:
        row = self._chapter_row(book_id, chapter_number)
        return decode_chapter_draft(row.draft_json)

    def load_plan(self, book_id: str, chapter_number: int) -> ChapterPlan:
        return decode_chapter_plan(self._chapter_row(book_id, chapter_number).plan_json)

    def load_delta(self, book_id: str, chapter_number: int) -> StoryStateDelta:
        return decode_story_state_delta(self._chapter_row(book_id, chapter_number).delta_json)

    def load_review(self, book_id: str, chapter_number: int, *, final: bool = False) -> ReviewReport:
        row = self._chapter_row(book_id, chapter_number)
        value = row.final_review_json if final else row.initial_review_json
        if value is None:
            raise ProjectPersistenceError(f"项目 {book_id} 第 {chapter_number} 章没有审查报告")
        return decode_review_report(value)

    def load_context_trace(self, book_id: str, chapter_number: int) -> ContextTrace:
        value = self._chapter_row(book_id, chapter_number).context_trace_json
        if value is None:
            raise ProjectPersistenceError(f"项目 {book_id} 第 {chapter_number} 章没有 Context Trace")
        return decode_context_trace(value)

    def save_plan_proposal(self, proposal: ChapterPlanProposal) -> None:
        encoded = _data(proposal)
        with self.database.session() as session:
            try:
                with session.begin():
                    self._require_book(session, proposal.book_id)
                    row = session.get(PlanProposalRow, proposal.proposal_id)
                    if row is not None:
                        current = decode_chapter_plan_proposal(row.proposal_json)
                        if proposal.book_id != current.book_id:
                            raise ProjectPersistenceError("候选计划不能切换作品")
                        if proposal.created_at != current.created_at:
                            raise ProjectPersistenceError("候选计划 created_at 不允许改变")
                        if proposal.version < current.version:
                            raise ProjectPersistenceError("不能用较旧版本覆盖候选计划")
                        row.version = proposal.version
                        row.chapter_number = proposal.chapter_number
                        row.base_chapter_number = proposal.base_chapter_number
                        row.status = proposal.status
                        row.updated_at = proposal.updated_at
                        row.proposal_json = encoded
                    else:
                        session.add(PlanProposalRow(
                            proposal_id=proposal.proposal_id,
                            book_id=proposal.book_id,
                            chapter_number=proposal.chapter_number,
                            base_chapter_number=proposal.base_chapter_number,
                            status=proposal.status,
                            version=proposal.version,
                            created_at=proposal.created_at,
                            updated_at=proposal.updated_at,
                            proposal_json=encoded,
                        ))
                    existing = session.scalar(select(PlanProposalVersionRow).where(
                        PlanProposalVersionRow.proposal_id == proposal.proposal_id,
                        PlanProposalVersionRow.version == proposal.version,
                    ))
                    if existing is None:
                        session.add(PlanProposalVersionRow(
                            proposal_id=proposal.proposal_id,
                            version=proposal.version,
                            created_at=proposal.updated_at,
                            proposal_json=encoded,
                        ))
            except IntegrityError as exc:
                raise ProjectPersistenceError(f"保存候选计划失败：{exc}") from exc

    def load_plan_proposal(self, book_id: str, proposal_id: str) -> ChapterPlanProposal:
        with self.database.session() as session:
            row = session.get(PlanProposalRow, proposal_id)
        if row is None or row.book_id != book_id:
            raise ProjectPersistenceError(f"候选章节计划不存在：{proposal_id}")
        return decode_chapter_plan_proposal(row.proposal_json)

    def save_chapter_candidate(
        self,
        *,
        proposal: ChapterPlanProposal,
        draft: ChapterDraft,
        final_draft: ChapterDraft,
        initial_review: ReviewReport,
        final_review: ReviewReport,
        context_trace: ContextTrace,
        reason: str,
        revised: bool,
        draft_history: tuple[ChapterDraft, ...] = (),
        review_history: tuple[ReviewReport, ...] = (),
    ) -> ChapterCandidateMetadata:
        state = self.load_state(proposal.book_id)
        expected = state.last_committed_chapter + 1
        if {proposal.chapter_number, proposal.plan.chapter_number, draft.chapter_number, final_draft.chapter_number, context_trace.chapter_number} != {expected}:
            raise ProjectPersistenceError("候选计划、正文和 Context Trace 必须使用当前下一章编号")
        self._validate_review_history(expected, draft_history, review_history)
        if not reason.strip():
            raise ValueError("候选草稿拒绝原因不能为空")
        metadata = ChapterCandidateMetadata(
            schema_version=1, candidate_id=str(uuid4()), proposal_id=proposal.proposal_id,
            book_id=proposal.book_id, chapter_number=expected, title=final_draft.title,
            word_count=final_draft.word_count, original_title=draft.title,
            original_word_count=draft.word_count, status="review_rejected", reason=reason.strip(),
            revised=revised, created_at=self._utc_now(),
        )
        with self.database.session() as session:
            with session.begin():
                session.add(ChapterCandidateRow(
                    candidate_id=metadata.candidate_id, book_id=metadata.book_id,
                    proposal_id=metadata.proposal_id, chapter_number=metadata.chapter_number,
                    status=metadata.status, reason=metadata.reason, revised=metadata.revised,
                    created_at=metadata.created_at,
                    metadata_json=_data(metadata), proposal_json=_data(proposal),
                    draft_json=_data(draft), final_draft_json=_data(final_draft),
                    initial_review_json=_data(initial_review), final_review_json=_data(final_review),
                    context_trace_json=_data(context_trace), draft_history_json=_data_list(draft_history),
                    review_history_json=_data_list(review_history),
                ))
        return metadata

    def load_chapter_candidate(self, book_id: str, candidate_id: str) -> ChapterCandidateMetadata:
        with self.database.session() as session:
            row = session.get(ChapterCandidateRow, candidate_id)
        if row is None or row.book_id != book_id:
            raise ProjectPersistenceError(f"候选章节不存在：{candidate_id}")
        return decode_chapter_candidate_metadata(row.metadata_json)

    def load_candidate_draft(self, book_id: str, candidate_id: str, *, final: bool = True) -> ChapterDraft:
        with self.database.session() as session:
            row = session.get(ChapterCandidateRow, candidate_id)
        if row is None or row.book_id != book_id:
            raise ProjectPersistenceError(f"候选章节不存在：{candidate_id}")
        return decode_chapter_draft(row.final_draft_json if final else row.draft_json)

    def commit_chapter(
        self,
        *,
        book_id: str,
        plan: ChapterPlan,
        draft: ChapterDraft,
        final_draft: ChapterDraft | None = None,
        initial_review: ReviewReport | None = None,
        final_review: ReviewReport | None = None,
        context_trace: ContextTrace | None = None,
        draft_history: tuple[ChapterDraft, ...] = (),
        review_history: tuple[ReviewReport, ...] = (),
        delta: StoryStateDelta,
        new_state: StoryState,
        status: str = "ready_for_review",
    ) -> ChapterMetadata:
        committed_draft = final_draft or draft
        with self.database.session() as session:
            try:
                with session.begin():
                    book = self._require_book(session, book_id, lock=True)
                    metadata = decode_book_metadata(book.metadata_json)
                    foundation = decode_novel_foundation(book.foundation_json)
                    current_state = decode_story_state(book.state_json)
                    expected = current_state.last_committed_chapter + 1
                    chapter_numbers = {plan.chapter_number, draft.chapter_number, committed_draft.chapter_number, delta.source_chapter, new_state.last_committed_chapter}
                    if chapter_numbers != {expected}:
                        raise ChapterCommitError(f"计划、正文、状态增量和新状态必须使用下一章编号 {expected}")
                    if new_state.book_id != book_id or new_state.schema_version != current_state.schema_version:
                        raise ChapterCommitError("新状态与当前作品状态不兼容")
                    if (initial_review is None) != (final_review is None):
                        raise ChapterCommitError("初始审查和最终审查必须同时提供")
                    if final_review is not None and status == "ready_for_review" and (not final_review.passed or final_review.parse_failed):
                        raise ChapterCommitError("未通过的最终审查不能标记为 ready_for_review")
                    if context_trace is not None and context_trace.chapter_number != expected:
                        raise ChapterCommitError("Context Trace 章节号与待提交章节不一致")
                    if self._reducer.apply(current_state, delta) != new_state:
                        raise ChapterCommitError("new_state 不是当前状态应用该增量后的结果")
                    character_ids = {item.character_id for item in foundation.characters}
                    unknown_characters = set(plan.participating_character_ids) - character_ids
                    if unknown_characters:
                        raise ChapterCommitError("章节计划引用了未知角色：" + ", ".join(sorted(unknown_characters)))
                    unknown_hooks = set(plan.relevant_hook_ids) - {item.hook_id for item in new_state.hooks}
                    if unknown_hooks:
                        raise ChapterCommitError("章节计划引用了未知伏笔：" + ", ".join(sorted(unknown_hooks)))
                    self._validate_review_history(expected, draft_history, review_history)
                    timestamp = self._utc_now()
                    chapter_metadata = ChapterMetadata(
                        chapter_number=expected, title=committed_draft.title,
                        file_name=f"{expected:04d}.md", word_count=committed_draft.word_count,
                        status=status, created_at=timestamp,
                    )
                    session.add(ChapterRow(
                        book_id=book_id, chapter_number=expected, metadata_json=_data(chapter_metadata),
                        draft_json=_data(committed_draft), original_draft_json=_data(draft),
                        plan_json=_data(plan), delta_json=_data(delta),
                        initial_review_json=_data(initial_review) if initial_review else None,
                        final_review_json=_data(final_review) if final_review else None,
                        context_trace_json=_data(context_trace) if context_trace else None,
                        draft_history_json=_data_list(draft_history), review_history_json=_data_list(review_history),
                    ))
                    session.add(StorySnapshotRow(book_id=book_id, chapter_number=expected, state_json=_data(new_state)))
                    book.metadata_json = _data(replace(metadata, updated_at=timestamp))
                    book.state_json = _data(new_state)
                    book.version += 1
                    return chapter_metadata
            except IntegrityError as exc:
                raise ChapterCommitError(f"章节提交冲突：{exc}") from exc

    def begin_chapter_rewrite(self, book_id: str, chapter_number: int) -> ChapterRewriteRecord:
        with self.database.session() as session:
            with session.begin():
                book = self._require_book(session, book_id, lock=True)
                state = decode_story_state(book.state_json)
                if chapter_number <= 0 or chapter_number > state.last_committed_chapter:
                    raise ValueError(f"重写章节必须在 1 到 {state.last_committed_chapter} 之间")
                record = ChapterRewriteRecord(
                    schema_version=1, rewrite_id=f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
                    book_id=book_id, chapter_number=chapter_number,
                    previous_last_chapter=state.last_committed_chapter,
                    archived_chapter_numbers=tuple(range(chapter_number, state.last_committed_chapter + 1)),
                    created_at=self._utc_now(),
                )
                archived_chapters = session.scalars(select(ChapterRow).where(
                    ChapterRow.book_id == book_id, ChapterRow.chapter_number >= chapter_number,
                )).all()
                archived_snapshots = session.scalars(select(StorySnapshotRow).where(
                    StorySnapshotRow.book_id == book_id, StorySnapshotRow.chapter_number >= chapter_number,
                )).all()
                base = session.scalar(select(StorySnapshotRow).where(
                    StorySnapshotRow.book_id == book_id, StorySnapshotRow.chapter_number == chapter_number - 1,
                ))
                if base is None:
                    raise ProjectPersistenceError("重写缺少上一章状态快照")
                archive = {
                    "metadata": book.metadata_json, "state": book.state_json,
                    "chapters": [self._chapter_payload(item) for item in archived_chapters],
                    "snapshots": [self._snapshot_payload(item) for item in archived_snapshots],
                }
                session.add(RewriteRow(rewrite_id=record.rewrite_id, book_id=book_id, record_json=_data(record), archive_json=archive, status="pending"))
                session.execute(delete(ChapterRow).where(ChapterRow.book_id == book_id, ChapterRow.chapter_number >= chapter_number))
                session.execute(delete(StorySnapshotRow).where(StorySnapshotRow.book_id == book_id, StorySnapshotRow.chapter_number >= chapter_number))
                book.state_json = base.state_json
                book.version += 1
                return record

    def finalize_chapter_rewrite(self, record: ChapterRewriteRecord) -> None:
        with self.database.session() as session:
            with session.begin():
                row = session.get(RewriteRow, record.rewrite_id)
                if row is None or row.book_id != record.book_id:
                    raise ProjectPersistenceError("章节重写记录不存在")
                row.status = "finalized"

    def rollback_chapter_rewrite(self, record: ChapterRewriteRecord) -> None:
        with self.database.session() as session:
            with session.begin():
                row = session.get(RewriteRow, record.rewrite_id)
                if row is None or row.book_id != record.book_id:
                    raise ProjectPersistenceError("章节重写记录不存在")
                if row.status != "pending":
                    return
                book = self._require_book(session, record.book_id, lock=True)
                archive = row.archive_json
                book.metadata_json = archive["metadata"]
                book.state_json = archive["state"]
                for payload in archive["chapters"]:
                    session.add(ChapterRow(**payload))
                for payload in archive["snapshots"]:
                    session.add(StorySnapshotRow(**payload))
                book.version += 1
                row.status = "rolled_back"

    def _book(self, book_id: str) -> BookRow:
        with self.database.session() as session:
            return self._require_book(session, book_id)

    def _chapter_rows(self, book_id: str) -> list[ChapterRow]:
        self._book(book_id)
        with self.database.session() as session:
            return session.scalars(select(ChapterRow).where(ChapterRow.book_id == book_id).order_by(ChapterRow.chapter_number)).all()

    def _chapter_row(self, book_id: str, chapter_number: int) -> ChapterRow:
        with self.database.session() as session:
            row = session.scalar(select(ChapterRow).where(ChapterRow.book_id == book_id, ChapterRow.chapter_number == chapter_number))
        if row is None:
            raise ProjectPersistenceError(f"项目 {book_id} 不存在第 {chapter_number} 章")
        return row

    @staticmethod
    def _require_book(session: Any, book_id: str, *, lock: bool = False) -> BookRow:
        statement = select(BookRow).where(BookRow.book_id == book_id)
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise ProjectNotFoundError(f"项目不存在：{book_id}")
        return row

    @staticmethod
    def _chapter_payload(row: ChapterRow) -> dict[str, object]:
        return {
            "book_id": row.book_id, "chapter_number": row.chapter_number,
            "metadata_json": row.metadata_json, "draft_json": row.draft_json,
            "original_draft_json": row.original_draft_json, "plan_json": row.plan_json,
            "delta_json": row.delta_json, "initial_review_json": row.initial_review_json,
            "final_review_json": row.final_review_json, "context_trace_json": row.context_trace_json,
            "draft_history_json": row.draft_history_json, "review_history_json": row.review_history_json,
        }

    @staticmethod
    def _snapshot_payload(row: StorySnapshotRow) -> dict[str, object]:
        return {"book_id": row.book_id, "chapter_number": row.chapter_number, "state_json": row.state_json}

    @staticmethod
    def _validate_initial_project(metadata: BookMetadata, foundation: NovelFoundation, state: StoryState) -> None:
        if state.book_id != metadata.book_id:
            raise ValueError("初始状态 book_id 与元数据不一致")
        if state.last_committed_chapter != 0:
            raise ValueError("初始状态 last_committed_chapter 必须为 0")
        if {item.character_id for item in state.characters} != {item.character_id for item in foundation.characters}:
            raise ValueError("初始角色状态必须覆盖基础资料中的全部角色")

    @staticmethod
    def _validate_review_history(chapter_number: int, drafts: tuple[ChapterDraft, ...], reviews: tuple[ReviewReport, ...]) -> None:
        if bool(drafts) != bool(reviews) or len(drafts) != len(reviews):
            raise ProjectPersistenceError("正文修订历史与审查历史必须一一对应")
        if any(item.chapter_number != chapter_number for item in drafts):
            raise ProjectPersistenceError("修订历史章节号不一致")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
