"""基于 JSON、JSONL 和 Markdown 的本地小说项目存储。"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .exceptions import (
    ChapterCommitError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ProjectPersistenceError,
)
from .models import (
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
from .serialization import (
    decode_book_metadata,
    decode_chapter_candidate_metadata,
    decode_chapter_index,
    decode_chapter_plan,
    decode_chapter_plan_proposal,
    decode_chapter_summary,
    decode_context_trace,
    decode_novel_foundation,
    decode_review_report,
    decode_story_state,
    decode_story_state_delta,
    dumps_json,
    load_json_file,
    loads_json,
    to_data,
)
from .state_reducer import NovelStateReducer


_BOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_PROPOSAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_UNSAFE_FILE_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


class NovelProjectStore:
    """将小说项目保存为可人工检查的本地文件。"""

    _PROJECT_DIRECTORIES = (
        "candidates",
        "chapters",
        "plans",
        "plans/pending",
        "reviews",
        "deltas",
        "summaries",
        "traces",
        "snapshots",
        "history/rewrites",
        "simulations",
        ".transactions",
    )

    def __init__(self, books_directory: str | Path) -> None:
        self.books_directory = Path(books_directory)
        self._reducer = NovelStateReducer()

    def create_project(
        self,
        *,
        metadata: BookMetadata,
        foundation: NovelFoundation,
        initial_state: StoryState,
    ) -> NovelProject:
        """通过 staging 目录创建项目，禁止覆盖相同 ``book_id``。"""

        self._validate_initial_project(metadata, foundation, initial_state)
        self.books_directory.mkdir(parents=True, exist_ok=True)
        project_directory = self._project_directory(metadata.book_id)
        if project_directory.exists():
            raise ProjectAlreadyExistsError(f"项目已存在：{metadata.book_id}")

        staging_directory = self.books_directory / (
            f".{metadata.book_id}.creating-{uuid4().hex}"
        )
        try:
            staging_directory.mkdir(parents=False, exist_ok=False)
            for directory_name in self._PROJECT_DIRECTORIES:
                (staging_directory / directory_name).mkdir(parents=True)

            self._write_text(
                staging_directory / "book.json",
                dumps_json(metadata),
            )
            self._write_text(
                staging_directory / "foundation.json",
                dumps_json(foundation),
            )
            self._write_text(
                staging_directory / "story_state.json",
                dumps_json(initial_state),
            )
            self._write_text(
                staging_directory / "chapter_index.json",
                dumps_json(()),
            )
            self._write_text(
                staging_directory / "facts.jsonl",
                self._facts_jsonl(initial_state),
            )
            self._write_text(
                staging_directory / "summaries" / "chapters.jsonl",
                "",
            )
            self._write_text(
                staging_directory / "snapshots" / "0000.json",
                dumps_json(initial_state),
            )

            if project_directory.exists():
                raise ProjectAlreadyExistsError(f"项目已存在：{metadata.book_id}")
            os.replace(staging_directory, project_directory)
        except ProjectAlreadyExistsError:
            self._remove_tree(staging_directory)
            raise
        except OSError as exc:
            self._remove_tree(staging_directory)
            raise ProjectPersistenceError(
                f"创建项目 {metadata.book_id} 失败：{exc}"
            ) from exc

        return NovelProject(metadata=metadata, foundation=foundation, state=initial_state)

    def load_project(self, book_id: str) -> NovelProject:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        return NovelProject(
            metadata=self._load_metadata_from(project_directory),
            foundation=self._load_foundation_from(project_directory),
            state=self._load_state_from(project_directory),
        )

    def list_projects(self) -> tuple[BookMetadata, ...]:
        """列出全部正式项目；不存在项目目录时返回空元组。"""

        if not self.books_directory.exists():
            return ()
        try:
            directories = sorted(
                (
                    path
                    for path in self.books_directory.iterdir()
                    if path.is_dir()
                    and not path.name.startswith(".")
                    and (path / "book.json").is_file()
                ),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise ProjectPersistenceError(
                f"无法列出小说项目目录 {self.books_directory}：{exc}"
            ) from exc

        projects: list[BookMetadata] = []
        for directory in directories:
            self._recover_transactions(directory)
            metadata = self._load_metadata_from(directory)
            if metadata.book_id != directory.name:
                raise ProjectPersistenceError(
                    f"项目目录名与 book_id 不一致：{directory.name}"
                )
            projects.append(metadata)
        return tuple(projects)

    def load_metadata(self, book_id: str) -> BookMetadata:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        return self._load_metadata_from(project_directory)

    def load_foundation(self, book_id: str) -> NovelFoundation:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        return self._load_foundation_from(project_directory)

    def load_state(self, book_id: str) -> StoryState:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        return self._load_state_from(project_directory)

    def load_snapshot(self, book_id: str, chapter_number: int) -> StoryState:
        if chapter_number < 0:
            raise ValueError("chapter_number 不能小于 0")
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        path = project_directory / "snapshots" / f"{chapter_number:04d}.json"
        if not path.is_file():
            raise ProjectPersistenceError(
                f"项目 {book_id} 不存在第 {chapter_number} 章快照"
            )
        return decode_story_state(load_json_file(path))

    def load_chapter_index(self, book_id: str) -> tuple[ChapterMetadata, ...]:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        return self._load_chapter_index_from(project_directory)

    def get_next_chapter_number(self, book_id: str) -> int:
        return self.load_state(book_id).last_committed_chapter + 1

    def begin_chapter_rewrite(
        self,
        book_id: str,
        chapter_number: int,
    ) -> ChapterRewriteRecord:
        """把指定章节及其后续正史归档，并暂时回退到上一章快照。

        调用方必须在成功准备新计划后调用 ``finalize_chapter_rewrite``；
        任一后续步骤失败时调用 ``rollback_chapter_rewrite`` 恢复原项目。
        """

        if not isinstance(chapter_number, int) or isinstance(chapter_number, bool):
            raise TypeError("chapter_number 必须是整数")
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        state = self._load_state_from(project_directory)
        if chapter_number <= 0 or chapter_number > state.last_committed_chapter:
            raise ValueError(
                f"重写章节必须在 1 到 {state.last_committed_chapter} 之间"
            )

        rewrite_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        record = ChapterRewriteRecord(
            schema_version=1,
            rewrite_id=rewrite_id,
            book_id=book_id,
            chapter_number=chapter_number,
            previous_last_chapter=state.last_committed_chapter,
            archived_chapter_numbers=tuple(
                range(chapter_number, state.last_committed_chapter + 1)
            ),
            created_at=self._utc_now(),
        )
        staging_directory = self.books_directory / (
            f".{book_id}.rewrite-{rewrite_id}.staging"
        )
        backup_directory = self._rewrite_backup_directory(record)
        try:
            shutil.copytree(project_directory, staging_directory)
            self._prepare_rewrite_staging(staging_directory, record)
            os.replace(project_directory, backup_directory)
            try:
                os.replace(staging_directory, project_directory)
            except OSError:
                os.replace(backup_directory, project_directory)
                raise
        except (OSError, ProjectPersistenceError, ValueError) as exc:
            self._remove_tree(staging_directory)
            if not project_directory.exists() and backup_directory.exists():
                os.replace(backup_directory, project_directory)
            if isinstance(exc, (ProjectPersistenceError, ValueError)):
                raise
            raise ProjectPersistenceError(f"准备第 {chapter_number} 章重写失败：{exc}") from exc
        return record

    def finalize_chapter_rewrite(self, record: ChapterRewriteRecord) -> None:
        """确认回退结果，并删除仅用于事务回滚的整项目备份。"""

        project_directory = self._project_directory(record.book_id)
        marker = self._load_rewrite_marker(project_directory, record)
        backup_directory = self.books_directory / marker["backup_directory"]
        try:
            self._remove_tree(backup_directory)
            (project_directory / ".rewrite-in-progress.json").unlink(missing_ok=False)
        except OSError as exc:
            raise ProjectPersistenceError(f"完成章节重写事务失败：{exc}") from exc

    def rollback_chapter_rewrite(self, record: ChapterRewriteRecord) -> None:
        """放弃尚未完成的重写，完整恢复重写前项目。"""

        project_directory = self._project_directory(record.book_id)
        marker = self._load_rewrite_marker(project_directory, record)
        backup_directory = self.books_directory / marker["backup_directory"]
        if not backup_directory.is_dir():
            raise ProjectPersistenceError("章节重写回滚备份不存在")
        discarded_directory = self.books_directory / (
            f".{record.book_id}.rewrite-{record.rewrite_id}.discarded"
        )
        try:
            os.replace(project_directory, discarded_directory)
            try:
                os.replace(backup_directory, project_directory)
            except OSError:
                os.replace(discarded_directory, project_directory)
                raise
            self._remove_tree(discarded_directory)
        except OSError as exc:
            raise ProjectPersistenceError(f"回滚章节重写失败：{exc}") from exc

    def load_chapter_summaries(self, book_id: str) -> tuple[ChapterSummary, ...]:
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        path = project_directory / "summaries" / "chapters.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ProjectPersistenceError(f"无法读取章节摘要 {path}：{exc}") from exc
        summaries = tuple(
            decode_chapter_summary(loads_json(line))
            for line in lines
            if line.strip()
        )
        numbers = tuple(summary.chapter_number for summary in summaries)
        if numbers != tuple(range(1, len(summaries) + 1)):
            raise ProjectPersistenceError("章节摘要编号不连续")
        state = self._load_state_from(project_directory)
        if len(summaries) != state.last_committed_chapter:
            raise ProjectPersistenceError("章节摘要数量与当前状态不一致")
        return summaries

    def load_chapter(self, book_id: str, chapter_number: int) -> ChapterDraft:
        index = self.load_chapter_index(book_id)
        metadata = next(
            (item for item in index if item.chapter_number == chapter_number),
            None,
        )
        if metadata is None:
            raise ProjectPersistenceError(
                f"项目 {book_id} 不存在第 {chapter_number} 章"
            )
        project_directory = self._existing_project_directory(book_id)
        path = project_directory / "chapters" / metadata.file_name
        try:
            document = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectPersistenceError(f"无法读取章节文件 {path}：{exc}") from exc
        prefix = f"# {metadata.title}\n\n"
        if not document.startswith(prefix):
            raise ProjectPersistenceError(f"章节文件格式无效：{path}")
        content = document[len(prefix) :].rstrip("\n")
        return ChapterDraft(
            chapter_number=chapter_number,
            title=metadata.title,
            content=content,
            word_count=metadata.word_count,
        )

    def load_plan(self, book_id: str, chapter_number: int) -> ChapterPlan:
        path = self._chapter_artifact_path(book_id, "plans", chapter_number, ".json")
        return decode_chapter_plan(load_json_file(path))

    def save_plan_proposal(self, proposal: ChapterPlanProposal) -> None:
        """原子保存候选计划，并为每个内容版本保留只读快照。"""

        project_directory = self._existing_project_directory(proposal.book_id)
        proposal_directory = self._proposal_directory(
            project_directory,
            proposal.proposal_id,
        )
        proposal_directory.mkdir(parents=True, exist_ok=True)
        current_path = proposal_directory / "current.json"
        if current_path.is_file():
            current = decode_chapter_plan_proposal(load_json_file(current_path))
            if proposal.version < current.version:
                raise ProjectPersistenceError("不能用较旧版本覆盖候选计划")
            if proposal.created_at != current.created_at:
                raise ProjectPersistenceError("候选计划 created_at 不允许改变")
            if proposal.book_id != current.book_id:
                raise ProjectPersistenceError("候选计划不能切换作品")

        serialized = dumps_json(proposal)
        version_path = proposal_directory / f"v{proposal.version:04d}.json"
        if not version_path.exists():
            self._atomic_write_text(version_path, serialized)
        self._atomic_write_text(current_path, serialized)

    def load_plan_proposal(
        self,
        book_id: str,
        proposal_id: str,
    ) -> ChapterPlanProposal:
        project_directory = self._existing_project_directory(book_id)
        path = self._proposal_directory(project_directory, proposal_id) / "current.json"
        if not path.is_file():
            raise ProjectPersistenceError(f"候选章节计划不存在：{proposal_id}")
        proposal = decode_chapter_plan_proposal(load_json_file(path))
        if proposal.book_id != book_id:
            raise ProjectPersistenceError("候选计划 book_id 与项目目录不一致")
        return proposal

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
        """保存未通过 strict 审稿的候选产物，不修改任何正史文件。"""

        project_directory = self._existing_project_directory(proposal.book_id)
        expected_chapter = self._load_state_from(
            project_directory
        ).last_committed_chapter + 1
        chapter_numbers = {
            proposal.chapter_number,
            proposal.plan.chapter_number,
            draft.chapter_number,
            final_draft.chapter_number,
            context_trace.chapter_number,
        }
        if chapter_numbers != {expected_chapter}:
            raise ProjectPersistenceError(
                "候选计划、正文和 Context Trace 必须使用当前下一章编号"
            )
        self._validate_review_history(
            chapter_number=expected_chapter,
            draft_history=draft_history,
            review_history=review_history,
        )
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("候选草稿拒绝原因不能为空")

        candidate_id = str(uuid4())
        metadata = ChapterCandidateMetadata(
            schema_version=1,
            candidate_id=candidate_id,
            proposal_id=proposal.proposal_id,
            book_id=proposal.book_id,
            chapter_number=proposal.chapter_number,
            title=final_draft.title,
            word_count=final_draft.word_count,
            original_title=draft.title,
            original_word_count=draft.word_count,
            status="review_rejected",
            reason=normalized_reason,
            revised=revised,
            created_at=self._utc_now(),
        )
        candidates_directory = project_directory / "candidates"
        candidates_directory.mkdir(parents=True, exist_ok=True)
        target_directory = candidates_directory / candidate_id
        staging_directory = candidates_directory / (
            f".{candidate_id}.creating-{uuid4().hex}"
        )
        try:
            staging_directory.mkdir(parents=False, exist_ok=False)
            self._write_text(
                staging_directory / "candidate.json",
                dumps_json(metadata),
            )
            self._write_text(
                staging_directory / "plan.json",
                dumps_json(proposal.plan),
            )
            self._write_text(
                staging_directory / "draft.md",
                self._draft_markdown(draft),
            )
            self._write_text(
                staging_directory / "final.md",
                self._draft_markdown(final_draft),
            )
            self._write_text(
                staging_directory / "review-initial.json",
                dumps_json(initial_review),
            )
            self._write_text(
                staging_directory / "review-final.json",
                dumps_json(final_review),
            )
            self._write_text(
                staging_directory / "context.json",
                dumps_json(context_trace),
            )
            if draft_history:
                rounds_directory = staging_directory / "rounds"
                rounds_directory.mkdir(parents=False, exist_ok=False)
                for round_number, (round_draft, round_review) in enumerate(
                    zip(draft_history, review_history, strict=True)
                ):
                    self._write_text(
                        rounds_directory / f"{round_number:02d}-draft.md",
                        self._draft_markdown(round_draft),
                    )
                    self._write_text(
                        rounds_directory / f"{round_number:02d}-review.json",
                        dumps_json(round_review),
                    )
            os.replace(staging_directory, target_directory)
        except OSError as exc:
            self._remove_tree(staging_directory)
            raise ProjectPersistenceError(
                f"保存章节候选草稿失败：{exc}"
            ) from exc
        return metadata

    def load_chapter_candidate(
        self,
        book_id: str,
        candidate_id: str,
    ) -> ChapterCandidateMetadata:
        path = self._candidate_directory(book_id, candidate_id) / "candidate.json"
        return decode_chapter_candidate_metadata(load_json_file(path))

    def load_candidate_draft(
        self,
        book_id: str,
        candidate_id: str,
        *,
        final: bool = True,
    ) -> ChapterDraft:
        metadata = self.load_chapter_candidate(book_id, candidate_id)
        file_name = "final.md" if final else "draft.md"
        path = self._candidate_directory(book_id, candidate_id) / file_name
        try:
            document = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectPersistenceError(f"无法读取候选正文 {path}：{exc}") from exc
        title = metadata.title if final else metadata.original_title
        word_count = metadata.word_count if final else metadata.original_word_count
        prefix = f"# {title}\n\n"
        if not document.startswith(prefix):
            raise ProjectPersistenceError(f"候选正文格式无效：{path}")
        return ChapterDraft(
            chapter_number=metadata.chapter_number,
            title=title,
            content=document[len(prefix) :].rstrip("\n"),
            word_count=word_count,
        )

    def load_delta(self, book_id: str, chapter_number: int) -> StoryStateDelta:
        path = self._chapter_artifact_path(book_id, "deltas", chapter_number, ".json")
        return decode_story_state_delta(load_json_file(path))

    def load_review(
        self,
        book_id: str,
        chapter_number: int,
        *,
        final: bool = False,
    ) -> ReviewReport:
        suffix = "-final.json" if final else "-initial.json"
        path = self._chapter_artifact_path(book_id, "reviews", chapter_number, suffix)
        return decode_review_report(load_json_file(path))

    def load_context_trace(self, book_id: str, chapter_number: int) -> ContextTrace:
        path = self._chapter_artifact_path(
            book_id,
            "traces",
            chapter_number,
            "-context.json",
        )
        return decode_context_trace(load_json_file(path))

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
        """把正文、增量和新状态作为一个可回滚事务提交。"""

        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        metadata = self._load_metadata_from(project_directory)
        foundation = self._load_foundation_from(project_directory)
        current_state = self._load_state_from(project_directory)
        chapter_index = self._load_chapter_index_from(project_directory)
        committed_draft = final_draft or draft

        expected_chapter = current_state.last_committed_chapter + 1
        chapter_numbers = {
            plan.chapter_number,
            draft.chapter_number,
            committed_draft.chapter_number,
            delta.source_chapter,
            new_state.last_committed_chapter,
        }
        if chapter_numbers != {expected_chapter}:
            raise ChapterCommitError(
                "计划、正文、状态增量和新状态必须使用下一章编号 "
                f"{expected_chapter}"
            )
        if any(item.chapter_number == expected_chapter for item in chapter_index):
            raise ChapterCommitError(f"第 {expected_chapter} 章已经提交")
        if new_state.book_id != book_id:
            raise ChapterCommitError("新状态的 book_id 与项目不一致")
        if new_state.schema_version != current_state.schema_version:
            raise ChapterCommitError("章节提交不能直接改变状态 schema_version")
        if (initial_review is None) != (final_review is None):
            raise ChapterCommitError("初始审查和最终审查必须同时提供")
        if context_trace is not None and context_trace.chapter_number != expected_chapter:
            raise ChapterCommitError("Context Trace 章节号与待提交章节不一致")
        if final_review is not None:
            if status == "ready_for_review" and (
                not final_review.passed or final_review.parse_failed
            ):
                raise ChapterCommitError("未通过的最终审查不能标记为 ready_for_review")

        expected_state = self._reducer.apply(current_state, delta)
        if new_state != expected_state:
            raise ChapterCommitError("new_state 不是当前状态应用该增量后的结果")

        character_ids = {character.character_id for character in foundation.characters}
        unknown_characters = set(plan.participating_character_ids) - character_ids
        if unknown_characters:
            names = ", ".join(sorted(unknown_characters))
            raise ChapterCommitError(f"章节计划引用了未知角色：{names}")
        hook_ids = {hook.hook_id for hook in new_state.hooks}
        unknown_hooks = set(plan.relevant_hook_ids) - hook_ids
        if unknown_hooks:
            names = ", ".join(sorted(unknown_hooks))
            raise ChapterCommitError(f"章节计划引用了未知伏笔：{names}")

        timestamp = self._utc_now()
        chapter_file_name = self._chapter_file_name(
            expected_chapter,
            committed_draft.title,
        )
        chapter_metadata = ChapterMetadata(
            chapter_number=expected_chapter,
            title=committed_draft.title,
            file_name=chapter_file_name,
            word_count=committed_draft.word_count,
            status=status,
            created_at=timestamp,
        )
        updated_metadata = replace(metadata, updated_at=timestamp)
        updated_index = (*chapter_index, chapter_metadata)

        relative_files = {
            Path("chapters") / chapter_file_name: (
                self._draft_markdown(committed_draft)
            ),
            Path("plans") / f"{expected_chapter:04d}.json": dumps_json(plan),
            Path("deltas") / f"{expected_chapter:04d}.json": dumps_json(delta),
            Path("snapshots") / f"{expected_chapter:04d}.json": dumps_json(new_state),
            Path("story_state.json"): dumps_json(new_state),
            Path("chapter_index.json"): dumps_json(updated_index),
            Path("book.json"): dumps_json(updated_metadata),
            Path("facts.jsonl"): self._facts_jsonl(new_state),
            Path("summaries") / "chapters.jsonl": self._append_summary(
                project_directory,
                expected_chapter,
                delta.chapter_summary,
            ),
        }
        if initial_review is not None and final_review is not None:
            relative_files[
                Path("reviews") / f"{expected_chapter:04d}-initial.json"
            ] = dumps_json(initial_review)
            relative_files[
                Path("reviews") / f"{expected_chapter:04d}-final.json"
            ] = dumps_json(final_review)
        self._validate_review_history(
            chapter_number=expected_chapter,
            draft_history=draft_history,
            review_history=review_history,
        )
        for round_number, (round_draft, round_review) in enumerate(
            zip(draft_history, review_history, strict=True)
        ):
            relative_files[
                Path("reviews")
                / f"{expected_chapter:04d}-draft-r{round_number:02d}.md"
            ] = self._draft_markdown(round_draft)
            relative_files[
                Path("reviews")
                / f"{expected_chapter:04d}-review-r{round_number:02d}.json"
            ] = dumps_json(round_review)
        if committed_draft != draft:
            relative_files[
                Path("reviews") / f"{expected_chapter:04d}-original.md"
            ] = self._draft_markdown(draft)
            relative_files[
                Path("reviews") / f"{expected_chapter:04d}-revised.md"
            ] = self._draft_markdown(committed_draft)
        if context_trace is not None:
            relative_files[
                Path("traces") / f"{expected_chapter:04d}-context.json"
            ] = dumps_json(context_trace)

        immutable_artifacts = tuple(
            path
            for path in relative_files
            if path.parts[0] in {
                "chapters",
                "plans",
                "reviews",
                "deltas",
                "traces",
                "snapshots",
            }
        )
        existing_artifacts = [
            str(path) for path in immutable_artifacts if (project_directory / path).exists()
        ]
        if existing_artifacts:
            raise ChapterCommitError(
                "章节产物已存在，拒绝覆盖：" + ", ".join(existing_artifacts)
            )

        self._commit_files(project_directory, relative_files)
        return chapter_metadata

    @staticmethod
    def _validate_review_history(
        *,
        chapter_number: int,
        draft_history: tuple[ChapterDraft, ...],
        review_history: tuple[ReviewReport, ...],
    ) -> None:
        """保证每一轮被审正文都有对应报告且章节号一致。"""

        if bool(draft_history) != bool(review_history):
            raise ProjectPersistenceError("正文历史和审稿历史必须同时提供")
        if len(draft_history) != len(review_history):
            raise ProjectPersistenceError("每一轮正文必须对应一份审稿报告")
        if any(item.chapter_number != chapter_number for item in draft_history):
            raise ProjectPersistenceError("正文历史包含其他章节")

    @staticmethod
    def _draft_markdown(draft: ChapterDraft) -> str:
        return f"# {draft.title}\n\n{draft.content.rstrip()}\n"

    def _commit_files(
        self,
        project_directory: Path,
        relative_files: dict[Path, str],
    ) -> None:
        transactions_directory = project_directory / ".transactions"
        transactions_directory.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid4().hex
        pending_directory = transactions_directory / f"pending-{transaction_id}"
        completed_directory = transactions_directory / f"completed-{transaction_id}"
        staged_directory = pending_directory / "new"
        backup_directory = pending_directory / "old"

        try:
            pending_directory.mkdir()
            targets: list[dict[str, object]] = []
            for relative_path, content in relative_files.items():
                target = project_directory / relative_path
                staged = staged_directory / relative_path
                self._write_text(staged, content)
                existed = target.is_file()
                targets.append({"path": relative_path.as_posix(), "existed": existed})
                if existed:
                    backup = backup_directory / relative_path
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)

            self._write_text(
                pending_directory / "manifest.json",
                json.dumps(
                    {"targets": targets},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )

            for relative_path in relative_files:
                target = project_directory / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                self._replace_staged_file(staged_directory / relative_path, target)

            # 重命名事务目录是提交点；completed 目录只用于崩溃后的垃圾清理。
            os.replace(pending_directory, completed_directory)
        except Exception as exc:
            if pending_directory.exists():
                self._rollback_transaction(project_directory, pending_directory)
            if isinstance(exc, ChapterCommitError):
                raise
            raise ChapterCommitError(f"章节事务提交失败：{exc}") from exc

        self._remove_tree(completed_directory)

    def _recover_transactions(self, project_directory: Path) -> None:
        transactions_directory = project_directory / ".transactions"
        if not transactions_directory.exists():
            return
        for transaction_directory in sorted(transactions_directory.iterdir()):
            if transaction_directory.name.startswith("pending-"):
                self._rollback_transaction(project_directory, transaction_directory)
            elif transaction_directory.name.startswith("completed-"):
                self._remove_tree(transaction_directory)

    def _rollback_transaction(
        self,
        project_directory: Path,
        transaction_directory: Path,
    ) -> None:
        manifest_path = transaction_directory / "manifest.json"
        if not manifest_path.is_file():
            self._remove_tree(transaction_directory)
            return
        try:
            manifest = load_json_file(manifest_path)
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("targets"), list
            ):
                raise ProjectPersistenceError("事务清单格式无效")
            targets = manifest["targets"]
            for item in reversed(targets):
                if not isinstance(item, dict):
                    raise ProjectPersistenceError("事务目标格式无效")
                relative_text = item.get("path")
                existed = item.get("existed")
                if not isinstance(relative_text, str) or not isinstance(existed, bool):
                    raise ProjectPersistenceError("事务目标字段无效")
                relative_path = self._safe_relative_path(relative_text)
                target = project_directory / relative_path
                if existed:
                    backup = transaction_directory / "old" / relative_path
                    if not backup.is_file():
                        raise ProjectPersistenceError(f"事务备份缺失：{relative_text}")
                    self._restore_backup(backup, target)
                elif target.exists():
                    target.unlink()
            self._remove_tree(transaction_directory)
        except OSError as exc:
            raise ProjectPersistenceError(f"回滚未完成事务失败：{exc}") from exc

    @staticmethod
    def _replace_staged_file(source: Path, target: Path) -> None:
        os.replace(source, target)

    @staticmethod
    def _restore_backup(backup: Path, target: Path) -> None:
        temporary = target.with_name(f".{target.name}.restore-{uuid4().hex}")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, temporary)
        os.replace(temporary, target)

    def _load_metadata_from(self, project_directory: Path) -> BookMetadata:
        return decode_book_metadata(load_json_file(project_directory / "book.json"))

    def _load_foundation_from(self, project_directory: Path) -> NovelFoundation:
        return decode_novel_foundation(
            load_json_file(project_directory / "foundation.json")
        )

    def _load_state_from(self, project_directory: Path) -> StoryState:
        return decode_story_state(
            load_json_file(project_directory / "story_state.json")
        )

    def _load_chapter_index_from(
        self,
        project_directory: Path,
    ) -> tuple[ChapterMetadata, ...]:
        index = decode_chapter_index(
            load_json_file(project_directory / "chapter_index.json")
        )
        numbers = tuple(item.chapter_number for item in index)
        if numbers != tuple(range(1, len(index) + 1)):
            raise ProjectPersistenceError("chapter_index 章节号不连续")
        return index

    def _chapter_artifact_path(
        self,
        book_id: str,
        directory_name: str,
        chapter_number: int,
        suffix: str,
    ) -> Path:
        if chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        project_directory = self._existing_project_directory(book_id)
        self._recover_transactions(project_directory)
        path = project_directory / directory_name / f"{chapter_number:04d}{suffix}"
        if not path.is_file():
            raise ProjectPersistenceError(
                f"项目 {book_id} 不存在第 {chapter_number} 章{directory_name}产物"
            )
        return path

    def _append_summary(
        self,
        project_directory: Path,
        chapter_number: int,
        summary: str,
    ) -> str:
        path = project_directory / "summaries" / "chapters.jsonl"
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            raise ProjectPersistenceError(f"无法读取章节摘要：{exc}") from exc
        line = json.dumps(
            {"chapter_number": chapter_number, "summary": summary},
            ensure_ascii=False,
            sort_keys=True,
        )
        return existing + ("" if not existing or existing.endswith("\n") else "\n") + line + "\n"

    @staticmethod
    def _facts_jsonl(state: StoryState) -> str:
        if not state.facts:
            return ""
        return "\n".join(
            json.dumps(to_data(fact), ensure_ascii=False, sort_keys=True)
            for fact in state.facts
        ) + "\n"

    @staticmethod
    def _chapter_file_name(chapter_number: int, title: str) -> str:
        slug = _UNSAFE_FILE_CHARS.sub("-", title.strip())
        slug = re.sub(r"\s+", "-", slug).strip(" .-")[:80]
        return f"{chapter_number:04d}-{slug or 'chapter'}.md"

    def _validate_initial_project(
        self,
        metadata: BookMetadata,
        foundation: NovelFoundation,
        initial_state: StoryState,
    ) -> None:
        self._validate_book_id(metadata.book_id)
        if initial_state.book_id != metadata.book_id:
            raise ValueError("初始状态的 book_id 与项目元数据不一致")
        if initial_state.schema_version != metadata.schema_version:
            raise ValueError("初始状态与项目元数据的 schema_version 不一致")
        if initial_state.last_committed_chapter != 0:
            raise ValueError("初始状态的 last_committed_chapter 必须为 0")
        profile_ids = {character.character_id for character in foundation.characters}
        state_ids = {character.character_id for character in initial_state.characters}
        if profile_ids != state_ids:
            raise ValueError("初始角色状态必须与基础人物资料一一对应")
        foundation_hook_ids = {hook.hook_id for hook in foundation.initial_hooks}
        state_hook_ids = {hook.hook_id for hook in initial_state.hooks}
        if foundation_hook_ids != state_hook_ids:
            raise ValueError("初始状态伏笔必须与基础资料一致")
        if any(fact.source_chapter != 0 for fact in initial_state.facts):
            raise ValueError("初始事实的 source_chapter 必须为 0")

    def _project_directory(self, book_id: str) -> Path:
        self._validate_book_id(book_id)
        return self.books_directory / book_id

    def _existing_project_directory(self, book_id: str) -> Path:
        project_directory = self._project_directory(book_id)
        if not project_directory.is_dir():
            backups = sorted(
                self.books_directory.glob(f".{book_id}.rewrite-*.previous")
            )
            if len(backups) == 1:
                try:
                    os.replace(backups[0], project_directory)
                except OSError as exc:
                    raise ProjectPersistenceError(
                        f"恢复中断的章节重写失败：{exc}"
                    ) from exc
        if not project_directory.is_dir():
            raise ProjectNotFoundError(f"项目不存在：{book_id}")
        self._recover_interrupted_rewrite(project_directory)
        return project_directory

    def _prepare_rewrite_staging(
        self,
        staging_directory: Path,
        record: ChapterRewriteRecord,
    ) -> None:
        """在隔离目录中构造回退后的权威状态和历史归档。"""

        chapter_index = self._load_chapter_index_from(staging_directory)
        rollback_state = decode_story_state(
            load_json_file(
                staging_directory
                / "snapshots"
                / f"{record.chapter_number - 1:04d}.json"
            )
        )
        summaries_path = staging_directory / "summaries" / "chapters.jsonl"
        try:
            summaries = tuple(
                decode_chapter_summary(loads_json(line))
                for line in summaries_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError as exc:
            raise ProjectPersistenceError(f"读取待回退章节摘要失败：{exc}") from exc

        archive_directory = (
            staging_directory / "history" / "rewrites" / record.rewrite_id
        )
        archive_directory.mkdir(parents=True, exist_ok=False)
        authority_directory = archive_directory / "authority"
        authority_directory.mkdir()
        for relative_path in (
            Path("book.json"),
            Path("story_state.json"),
            Path("chapter_index.json"),
            Path("facts.jsonl"),
            Path("summaries/chapters.jsonl"),
        ):
            source = staging_directory / relative_path
            if source.is_file():
                destination = authority_directory / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        retained_index = tuple(
            item
            for item in chapter_index
            if item.chapter_number < record.chapter_number
        )
        for item in chapter_index:
            if item.chapter_number >= record.chapter_number:
                self._archive_rewrite_path(
                    staging_directory,
                    archive_directory,
                    Path("chapters") / item.file_name,
                )

        numbered_patterns = {
            "plans": re.compile(r"^(\d{4})\.json$"),
            "reviews": re.compile(r"^(\d{4})-"),
            "deltas": re.compile(r"^(\d{4})\.json$"),
            "traces": re.compile(r"^(\d{4})-"),
            "snapshots": re.compile(r"^(\d{4})\.json$"),
        }
        for directory_name, pattern in numbered_patterns.items():
            directory = staging_directory / directory_name
            for path in tuple(directory.iterdir()):
                if not path.is_file():
                    continue
                matched = pattern.match(path.name)
                if matched and int(matched.group(1)) >= record.chapter_number:
                    self._archive_rewrite_path(
                        staging_directory,
                        archive_directory,
                        path.relative_to(staging_directory),
                    )

        # 所有待确认计划、候选稿和模拟都基于旧时间线，回退后不能继续使用。
        for relative_directory in (
            Path("plans/pending"),
            Path("candidates"),
            Path("simulations"),
        ):
            directory = staging_directory / relative_directory
            # 早期版本创建的项目可能还没有候选计划或候选稿目录。
            # 重写必须兼容这些旧项目，并为后续保存新计划补齐目录。
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                continue
            if not directory.is_dir():
                raise ProjectPersistenceError(
                    f"重写所需路径不是目录：{relative_directory.as_posix()}"
                )
            for path in tuple(directory.iterdir()):
                self._archive_rewrite_path(
                    staging_directory,
                    archive_directory,
                    path.relative_to(staging_directory),
                )

        metadata = self._load_metadata_from(staging_directory)
        self._write_text(
            staging_directory / "book.json",
            dumps_json(replace(metadata, updated_at=record.created_at)),
        )
        self._write_text(
            staging_directory / "story_state.json",
            dumps_json(rollback_state),
        )
        self._write_text(
            staging_directory / "chapter_index.json",
            dumps_json(retained_index),
        )
        self._write_text(
            staging_directory / "facts.jsonl",
            self._facts_jsonl(rollback_state),
        )
        retained_summaries = tuple(
            item
            for item in summaries
            if item.chapter_number < record.chapter_number
        )
        summary_text = "".join(
            json.dumps(to_data(item), ensure_ascii=False, sort_keys=True) + "\n"
            for item in retained_summaries
        )
        self._write_text(summaries_path, summary_text)
        self._write_text(
            archive_directory / "rewrite.json",
            dumps_json(record),
        )
        self._write_text(
            staging_directory / ".rewrite-in-progress.json",
            json.dumps(
                {
                    **to_data(record),
                    "backup_directory": self._rewrite_backup_directory(record).name,
                    "process_id": os.getpid(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    @staticmethod
    def _archive_rewrite_path(
        staging_directory: Path,
        archive_directory: Path,
        relative_path: Path,
    ) -> None:
        source = staging_directory / relative_path
        if not source.exists():
            return
        destination = archive_directory / "artifacts" / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)

    def _rewrite_backup_directory(self, record: ChapterRewriteRecord) -> Path:
        return self.books_directory / (
            f".{record.book_id}.rewrite-{record.rewrite_id}.previous"
        )

    def _load_rewrite_marker(
        self,
        project_directory: Path,
        record: ChapterRewriteRecord,
    ) -> dict[str, object]:
        marker_path = project_directory / ".rewrite-in-progress.json"
        if not marker_path.is_file():
            raise ProjectPersistenceError("章节重写事务标记不存在")
        marker = load_json_file(marker_path)
        if not isinstance(marker, dict):
            raise ProjectPersistenceError("章节重写事务标记格式无效")
        if marker.get("rewrite_id") != record.rewrite_id:
            raise ProjectPersistenceError("章节重写事务标记与记录不一致")
        backup_name = marker.get("backup_directory")
        if (
            not isinstance(backup_name, str)
            or Path(backup_name).name != backup_name
            or not backup_name.startswith(f".{record.book_id}.rewrite-")
        ):
            raise ProjectPersistenceError("章节重写备份目录名不安全")
        return marker

    def _recover_interrupted_rewrite(self, project_directory: Path) -> None:
        marker_path = project_directory / ".rewrite-in-progress.json"
        if not marker_path.is_file():
            return
        marker = load_json_file(marker_path)
        if not isinstance(marker, dict):
            raise ProjectPersistenceError("章节重写事务标记格式无效")
        process_id = marker.get("process_id")
        if isinstance(process_id, int) and self._process_exists(process_id):
            return
        backup_name = marker.get("backup_directory")
        if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
            raise ProjectPersistenceError("中断的章节重写备份目录名无效")
        backup_directory = self.books_directory / backup_name
        if not backup_directory.is_dir():
            # 备份已清理表示事务已完成，只残留了标记文件。
            marker_path.unlink(missing_ok=True)
            return
        discarded = self.books_directory / f".{project_directory.name}.rewrite-recovery"
        self._remove_tree(discarded)
        try:
            os.replace(project_directory, discarded)
            os.replace(backup_directory, project_directory)
        except OSError as exc:
            if not project_directory.exists() and discarded.exists():
                os.replace(discarded, project_directory)
            raise ProjectPersistenceError(f"恢复中断的章节重写失败：{exc}") from exc
        self._remove_tree(discarded)

    @staticmethod
    def _process_exists(process_id: int) -> bool:
        if process_id <= 0:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _proposal_directory(project_directory: Path, proposal_id: str) -> Path:
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_PATTERN.fullmatch(
            proposal_id
        ):
            raise ValueError("proposal_id 格式不合法")
        return project_directory / "plans" / "pending" / proposal_id

    def _candidate_directory(self, book_id: str, candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or not _PROPOSAL_ID_PATTERN.fullmatch(
            candidate_id
        ):
            raise ValueError("candidate_id 格式不合法")
        path = self._existing_project_directory(book_id) / "candidates" / candidate_id
        if not path.is_dir():
            raise ProjectPersistenceError(f"候选章节不存在：{candidate_id}")
        return path

    @staticmethod
    def _validate_book_id(book_id: str) -> None:
        if not isinstance(book_id, str) or not _BOOK_ID_PATTERN.fullmatch(book_id):
            raise ValueError(
                "book_id 只能包含字母、数字、下划线和连字符，长度为 1 到 80"
            )

    @staticmethod
    def _safe_relative_path(value: str) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ProjectPersistenceError(f"事务包含不安全路径：{value}")
        return path

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
