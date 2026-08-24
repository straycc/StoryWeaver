"""创建小说项目的确定性应用 Pipeline。"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from ..memory.services import LongTermMemoryRetriever
from ..observability import get_log_context, logging_context
from ..context_management import trace_from_selected_entries

from .context_builder import ChapterContextBuilder
from .context_renderer import ChapterContextRenderer
from .exceptions import ChapterPipelineError
from .models import (
    BatchPlanningContext,
    BookMetadata,
    ChapterContext,
    ChapterDraft,
    ChapterPlan,
    ChapterPlanProposal,
    ChapterResult,
    CharacterState,
    CreateNovelRequest,
    NovelFoundation,
    NovelProject,
    ReviewReport,
    StoryState,
    StoryStateDelta,
)
from .project_store import NovelProjectStore
from .quality_gate import (
    ReviewDecision,
    ReviewGateResult,
    ReviewQualityGate,
)
from .serialization import dumps_json, to_data
from .state_reducer import NovelStateReducer
from .validation import ChapterDraftValidator, NovelFoundationValidator


_LOGGER = logging.getLogger(__name__)


class Architect(Protocol):
    """创建 Pipeline 所依赖的最小 Architect 接口。"""

    async def create(self, request: CreateNovelRequest) -> NovelFoundation:
        """根据创作简报返回候选小说基础资料。"""


class Planner(Protocol):
    async def plan(
        self,
        *,
        project: NovelProject,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
    ) -> ChapterPlan:
        """规划项目的下一章。"""


class Writer(Protocol):
    async def write(self, context: ChapterContext) -> ChapterDraft:
        """根据有限 Context 生成章节初稿。"""


class Reviewer(Protocol):
    async def review(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
    ) -> ReviewReport:
        """审查章节正文。"""

    async def verify_revision(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        original_review: ReviewReport,
    ) -> ReviewReport:
        """只复查上一轮要求修复的问题及明显回归。"""


class Reviser(Protocol):
    async def revise(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        review: ReviewReport,
    ) -> ChapterDraft:
        """根据审查报告修订一次正文。"""


class ChapterAnalyzer(Protocol):
    async def analyze(
        self,
        *,
        project: NovelProject,
        plan: ChapterPlan,
        draft: ChapterDraft,
    ) -> StoryStateDelta:
        """从最终正文提取候选状态增量。"""


class CreateNovelPipeline:
    """编排 Architect、确定性初始化和项目原子发布。"""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        architect: Architect,
        store: NovelProjectStore,
        validator: NovelFoundationValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._architect = architect
        self._store = store
        self._validator = validator or NovelFoundationValidator()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, request: CreateNovelRequest) -> NovelProject:
        """创建并发布项目；Architect 或校验失败时不会触碰正式目录。"""

        foundation = await self._architect.create(request)
        if not isinstance(foundation, NovelFoundation):
            raise TypeError("Architect.create 必须返回 NovelFoundation")
        self._validator.validate(request=request, foundation=foundation)

        book_id = self.build_book_id(request)
        timestamp = self._timestamp()
        metadata = BookMetadata(
            schema_version=self.SCHEMA_VERSION,
            book_id=book_id,
            title=request.title,
            genre=request.genre,
            target_chapters=request.target_chapters,
            chapter_target_words=request.chapter_target_words,
            language=request.language,
            created_at=timestamp,
            updated_at=timestamp,
        )
        initial_state = self._build_initial_state(
            book_id=book_id,
            foundation=foundation,
        )
        return self._store.create_project(
            metadata=metadata,
            foundation=foundation,
            initial_state=initial_state,
        )

    @classmethod
    def build_book_id(cls, request: CreateNovelRequest) -> str:
        """根据规范化简报生成路径安全且可重复的项目 ID。"""

        normalized_title = unicodedata.normalize("NFKD", request.title)
        ascii_title = normalized_title.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-")
        canonical_request = dumps_json(to_data(request), pretty=False)
        digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()[:12]
        return f"{(slug or 'novel')[:40]}-{digest}"

    def _build_initial_state(
        self,
        *,
        book_id: str,
        foundation: NovelFoundation,
    ) -> StoryState:
        characters = tuple(
            CharacterState(
                character_id=profile.character_id,
                location="未指定起始地点",
                status="尚未登场",
                current_goal=profile.long_term_goal,
                emotion="平静",
                possessions=(),
                known_fact_ids=(),
            )
            for profile in foundation.characters
        )
        return StoryState(
            schema_version=self.SCHEMA_VERSION,
            book_id=book_id,
            last_committed_chapter=0,
            current_time="故事开始前",
            current_location="未指定起始地点",
            characters=characters,
            facts=(),
            hooks=foundation.initial_hooks,
        )

    def _timestamp(self) -> str:
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("CreateNovelPipeline 的 clock 必须返回带时区时间")
        return current.astimezone(timezone.utc).isoformat()


class WriteNextChapterPipeline:
    """编排一次完整且可原子提交的“写下一章”流程。"""

    REVIEW_POLICIES = frozenset({"strict", "auto"})

    def __init__(
        self,
        *,
        store: NovelProjectStore,
        planner: Planner,
        context_builder: ChapterContextBuilder,
        writer: Writer,
        reviewer: Reviewer,
        reviser: Reviser,
        analyzer: ChapterAnalyzer,
        draft_validator: ChapterDraftValidator | None = None,
        state_reducer: NovelStateReducer | None = None,
        memory_retriever: LongTermMemoryRetriever | None = None,
        creative_control_provider: Callable[[str], object] | None = None,
        context_snapshot_sink: object | None = None,
        review_policy: str = "strict",
        quality_gate: ReviewQualityGate | None = None,
    ) -> None:
        if review_policy not in self.REVIEW_POLICIES:
            raise ValueError(f"不支持的审稿提交策略：{review_policy}")
        self._store = store
        self._planner = planner
        self._context_builder = context_builder
        self._writer = writer
        self._reviewer = reviewer
        self._reviser = reviser
        self._analyzer = analyzer
        self._draft_validator = draft_validator or ChapterDraftValidator()
        self._state_reducer = state_reducer or NovelStateReducer()
        self._memory_retriever = memory_retriever
        self._creative_control_provider = creative_control_provider
        # 采用鸭子类型，领域 Pipeline 不反向依赖 PostgreSQL ORM。
        self._context_snapshot_sink = context_snapshot_sink
        self._review_policy = review_policy
        self._quality_gate = quality_gate or ReviewQualityGate()

    async def run(
        self,
        *,
        book_id: str,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
    ) -> ChapterResult:
        proposal = await self.prepare(
            book_id=book_id,
            user_instruction=user_instruction,
            batch_context=batch_context,
        )
        return await self.execute(proposal)

    async def prepare(
        self,
        *,
        book_id: str,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
    ) -> ChapterPlanProposal:
        """只规划下一章，不生成正文。"""

        project = self._store.load_project(book_id)
        planner_instruction = self._planner_instruction(book_id, user_instruction, project.state.last_committed_chapter + 1)
        plan_arguments: dict[str, object] = {
            "project": project,
            "user_instruction": planner_instruction,
        }
        # 保持现有测试替身和第三方 Planner 的兼容；只有连续创作才需要新边界。
        if batch_context is not None:
            plan_arguments["batch_context"] = batch_context
        plan = await self._planner.plan(**plan_arguments)
        if not isinstance(plan, ChapterPlan):
            raise ChapterPipelineError("Planner 必须返回 ChapterPlan")
        self._log_stage_summary(
            "novel-planner",
            "计划结果 | 第 %d 章 · %s",
            plan.chapter_number,
            self._short_text(plan.goal),
        )
        long_term_memories = await self._retrieve_memories(
            book_id=book_id,
            plan=plan,
            user_instruction=user_instruction,
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        return ChapterPlanProposal(
            proposal_id=str(uuid4()),
            book_id=book_id,
            chapter_number=plan.chapter_number,
            base_chapter_number=project.state.last_committed_chapter,
            base_current_time=project.state.current_time,
            version=1,
            status="pending",
            plan=plan,
            user_instruction=user_instruction,
            feedback_history=(),
            selected_memory_ids=tuple(
                item.memory_id for item in long_term_memories
            ),
            selected_memory_descriptions=tuple(
                f"{item.name}：{item.description}" for item in long_term_memories
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )

    async def revise(
        self,
        proposal: ChapterPlanProposal,
        *,
        feedback: str,
    ) -> ChapterPlanProposal:
        """基于用户反馈生成新的候选计划版本。"""

        normalized_feedback = feedback.strip()
        if not normalized_feedback:
            raise ValueError("计划修改意见不能为空")
        project = self._validate_proposal_base(proposal)
        revision_instruction = (
            "请修订当前候选章节计划。保留用户未要求改变的内容，"
            "但最终仍需返回完整 ChapterPlan。\n\n"
            f"原始本章要求：{proposal.user_instruction or '无'}\n"
            f"当前候选计划：\n{dumps_json(to_data(proposal.plan))}\n"
            f"本次修改意见：{normalized_feedback}"
        )
        plan = await self._planner.plan(
            project=project,
            user_instruction=self._planner_instruction(proposal.book_id, revision_instruction, proposal.chapter_number),
        )
        if not isinstance(plan, ChapterPlan):
            raise ChapterPipelineError("Planner 必须返回 ChapterPlan")
        self._log_stage_summary(
            "novel-planner",
            "计划修订 | 第 %d 章 V%d · %s",
            plan.chapter_number,
            proposal.version + 1,
            self._short_text(plan.goal),
        )
        long_term_memories = await self._retrieve_memories(
            book_id=proposal.book_id,
            plan=plan,
            user_instruction=self._effective_instruction(
                proposal,
                additional_feedback=normalized_feedback,
            ),
        )
        return replace(
            proposal,
            version=proposal.version + 1,
            plan=plan,
            feedback_history=(*proposal.feedback_history, normalized_feedback),
            selected_memory_ids=tuple(
                item.memory_id for item in long_term_memories
            ),
            selected_memory_descriptions=tuple(
                f"{item.name}：{item.description}" for item in long_term_memories
            ),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def execute(
        self,
        proposal: ChapterPlanProposal,
        *,
        quality_gate: ReviewQualityGate | None = None,
    ) -> ChapterResult:
        """使用用户已查看的候选计划执行正文、审查和状态提交。"""

        project = self._validate_proposal_base(proposal)
        active_quality_gate = quality_gate or self._quality_gate
        summaries = self._store.load_chapter_summaries(proposal.book_id)
        long_term_memories = ()
        if self._memory_retriever is not None:
            long_term_memories = self._memory_retriever.load_selected(
                memory_ids=proposal.selected_memory_ids,
                book_id=proposal.book_id,
            )
        context, context_trace = self._context_builder.build(
            project=project,
            plan=proposal.plan,
            chapter_summaries=summaries,
            user_instruction=self._effective_instruction(proposal),
            long_term_memories=long_term_memories,
        )
        self._log_stage_summary(
            "pipeline",
            "上下文 | %d 个来源 · 估算 %s Token · 长期记忆 %d 条",
            len(context.entries),
            f"{context.estimated_tokens:,}",
            len(long_term_memories),
        )
        self._record_writer_context_snapshot(
            project=project,
            context=context,
            context_trace=context_trace,
        )

        draft = await self._writer.write(context)
        if not isinstance(draft, ChapterDraft):
            raise ChapterPipelineError("Writer 必须返回 ChapterDraft")
        draft = self._draft_validator.validate_and_normalize(
            context=context,
            draft=draft,
        )
        self._log_stage_summary(
            "novel-writer",
            "正文结果 | 《%s》 · %d 字",
            draft.title,
            draft.word_count,
        )

        draft_history = [draft]
        review_history: list[ReviewReport] = []
        previous_review: ReviewReport | None = None
        revision_count = 0
        gate_result: ReviewGateResult | None = None

        while True:
            final_draft = draft_history[-1]
            if previous_review is None:
                final_review = await self._reviewer.review(
                    context=context,
                    draft=final_draft,
                )
            else:
                final_review = await self._reviewer.verify_revision(
                    context=context,
                    draft=final_draft,
                    original_review=previous_review,
                )
            if not isinstance(final_review, ReviewReport):
                raise ChapterPipelineError("Reviewer 必须返回 ReviewReport")
            review_history.append(final_review)
            gate_result = active_quality_gate.evaluate(
                review=final_review,
                actual_words=final_draft.word_count,
                target_words=project.metadata.chapter_target_words,
                revision_round=revision_count,
                previous_review=previous_review,
            )
            self._log_stage_summary(
                "novel-reviewer",
                "审查结果 | %s · %s · %s",
                self._review_score(final_review),
                self._review_issue_counts(final_review),
                self._short_text(final_review.summary),
            )
            self._log_stage_summary(
                "pipeline",
                "质量门禁 | %s%s",
                self._gate_label(gate_result.decision),
                f" · {self._short_text('；'.join(gate_result.reasons))}"
                if gate_result.reasons
                else "",
            )
            if gate_result.decision is ReviewDecision.ACCEPT:
                break
            if gate_result.decision is ReviewDecision.REJECT:
                break

            revision_review = replace(
                final_review,
                issues=gate_result.actionable_issues,
            )
            revised_candidate = await self._reviser.revise(
                context=context,
                draft=final_draft,
                review=revision_review,
            )
            if not isinstance(revised_candidate, ChapterDraft):
                raise ChapterPipelineError("Reviser 必须返回 ChapterDraft")
            revised_candidate = self._draft_validator.validate_and_normalize(
                context=context,
                draft=revised_candidate,
            )
            self._log_stage_summary(
                "novel-reviser",
                "修订结果 | 第 %d 轮 · 《%s》 · %d 字",
                revision_count + 1,
                revised_candidate.title,
                revised_candidate.word_count,
            )
            if revised_candidate == final_draft:
                gate_result = ReviewGateResult(
                    decision=ReviewDecision.REJECT,
                    reasons=("Reviser 未产生任何正文变化，停止重复修订",),
                    actionable_issues=gate_result.actionable_issues,
                )
                break
            draft_history.append(revised_candidate)
            revision_count += 1
            # 复查只需要知道实际交给 Reviser 的硬问题，避免把风格观察项
            # 误当成下一轮必须复现的阻断问题。
            previous_review = revision_review

        if gate_result is None:  # pragma: no cover - while 必须产生门禁结果
            raise AssertionError("Review Loop 未产生 QualityGate 结果")
        initial_review = review_history[0]
        final_review = review_history[-1]
        final_draft = draft_history[-1]
        revised = revision_count > 0
        rejection_reason = "；".join(gate_result.reasons)
        if (
            self._review_policy == "strict"
            and gate_result.decision is ReviewDecision.REJECT
        ):
            candidate = self._store.save_chapter_candidate(
                proposal=proposal,
                draft=draft,
                final_draft=final_draft,
                initial_review=initial_review,
                final_review=final_review,
                context_trace=context_trace,
                reason=rejection_reason,
                revised=revised,
                draft_history=tuple(draft_history),
                review_history=tuple(review_history),
            )
            self._log_stage_summary(
                "pipeline",
                "候选已保存 | %s · strict 未提交 · %s",
                candidate.candidate_id[:8],
                self._short_text(rejection_reason),
            )
            return ChapterResult(
                chapter_number=proposal.plan.chapter_number,
                plan=proposal.plan,
                draft=draft,
                final_draft=final_draft,
                initial_review=initial_review,
                final_review=final_review,
                revised=revised,
                state_delta=None,
                context_trace=context_trace,
                status="draft_rejected",
                committed=False,
                candidate_id=candidate.candidate_id,
                revision_count=revision_count,
                draft_history=tuple(draft_history),
                review_history=tuple(review_history),
            )

        delta = await self._analyzer.analyze(
            project=project,
            plan=proposal.plan,
            draft=final_draft,
        )
        if not isinstance(delta, StoryStateDelta):
            raise ChapterPipelineError("ChapterAnalyzer 必须返回 StoryStateDelta")
        self._log_stage_summary(
            "chapter-analyzer",
            "状态结果 | 新事实 %d · 新伏笔 %d · 人物更新 %d",
            len(delta.new_facts),
            len(delta.new_hooks),
            len(delta.character_updates),
        )
        new_state = self._state_reducer.apply(project.state, delta)
        status = (
            "ready_for_review"
            if gate_result.decision is ReviewDecision.ACCEPT
            else "review_warning"
        )

        self._store.commit_chapter(
            book_id=proposal.book_id,
            plan=proposal.plan,
            draft=draft,
            final_draft=final_draft,
            initial_review=initial_review,
            final_review=final_review,
            context_trace=context_trace,
            draft_history=tuple(draft_history),
            review_history=tuple(review_history),
            delta=delta,
            new_state=new_state,
            status=status,
        )
        self._log_stage_summary(
            "pipeline",
            "✓ 已提交第 %d 章《%s》 · %s · 自动修订 %d 轮",
            final_draft.chapter_number,
            final_draft.title,
            status,
            revision_count,
        )
        return ChapterResult(
            chapter_number=proposal.plan.chapter_number,
            plan=proposal.plan,
            draft=draft,
            final_draft=final_draft,
            initial_review=initial_review,
            final_review=final_review,
            revised=revised,
            state_delta=delta,
            context_trace=context_trace,
            status=status,
            revision_count=revision_count,
            draft_history=tuple(draft_history),
            review_history=tuple(review_history),
        )

    def _planner_instruction(self, book_id: str, user_instruction: str | None, chapter_number: int) -> str | None:
        """控制面是受保护编译输入，不篡改 Proposal 中保存的原始用户要求。"""

        if self._creative_control_provider is None:
            return user_instruction
        control = self._creative_control_provider(book_id)
        author_intent = str(getattr(control, "author_intent", "")).strip()
        current_focus = str(getattr(control, "current_focus", "")).strip()
        if str(getattr(control, "current_focus_mode", "persistent")) == "single_chapter" and getattr(control, "focus_target_chapter", None) != chapter_number:
            current_focus = ""
        if not author_intent and not current_focus:
            return user_instruction
        control_text = (
            "[受保护创作控制：必须遵守，不能把它当作故事正文]\n"
            f"作者意图：{author_intent or '未设置'}\n"
            f"当前焦点：{current_focus or '未设置'}"
        )
        return f"{user_instruction or '无额外用户要求'}\n\n{control_text}"

    async def _retrieve_memories(
        self,
        *,
        book_id: str,
        plan: ChapterPlan,
        user_instruction: str | None,
    ) -> tuple[object, ...]:
        if self._memory_retriever is None:
            return ()
        memory_query = " ".join(
            (
                user_instruction or "",
                plan.goal,
                plan.location,
                plan.ending_hook,
                *plan.required_beats,
                *plan.style_focus,
            )
        )
        return await self._memory_retriever.retrieve(
            query=memory_query,
            book_id=book_id,
        )

    def _record_writer_context_snapshot(
        self,
        *,
        project: NovelProject,
        context: ChapterContext,
        context_trace: object,
    ) -> None:
        """冻结 Writer 实际看到的上下文；审计失败不应中断写作。"""

        sink = self._context_snapshot_sink
        if sink is None:
            return
        try:
            loader = getattr(self._store, "load_project_with_version", None)
            book_version = (
                loader(project.metadata.book_id)[1]
                if callable(loader)
                else project.state.last_committed_chapter
            )
            trace = trace_from_selected_entries(
                agent_role="writer",
                policy_version="writer-context-v2.1",
                book_version=book_version,
                token_budget=getattr(context_trace, "budget", context.estimated_tokens),
                entries=context.entries,
                excluded_source_ids=tuple(
                    getattr(context_trace, "excluded_source_ids", ())
                ),
                notes=tuple(getattr(context_trace, "notes", ())),
            )
            sink.save(
                agent_role="writer",
                book_id=project.metadata.book_id,
                book_version=book_version,
                policy_version=trace.policy_version,
                renderer_version=trace.renderer_version,
                rendered_context=ChapterContextRenderer().render(context),
                trace=trace.to_data(),
                job_id=get_log_context().get("run_id"),
            )
        except Exception:
            _LOGGER.warning("Writer Context Snapshot 写入失败", exc_info=True)

    @staticmethod
    def _log_stage_summary(agent_id: str, message: str, *args: object) -> None:
        """输出可读的业务摘要；文件 Handler 会额外保留完整调用关联字段。"""

        with logging_context(agent_id=agent_id):
            _LOGGER.info(message, *args)

    @staticmethod
    def _short_text(value: str, *, limit: int = 100) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit - 1]}…"

    @staticmethod
    def _review_score(review: ReviewReport) -> str:
        return f"评分 {review.score}" if review.score is not None else "未评分"

    @staticmethod
    def _review_issue_counts(review: ReviewReport) -> str:
        if not review.issues:
            return "无问题"
        counts: dict[str, int] = {}
        for issue in review.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return " · ".join(
            f"{severity} {counts[severity]}"
            for severity in ("critical", "warning", "info")
            if severity in counts
        )

    @staticmethod
    def _gate_label(decision: ReviewDecision) -> str:
        labels = {
            ReviewDecision.ACCEPT: "通过",
            ReviewDecision.REVISE: "需要修订",
            ReviewDecision.REJECT: "拒绝",
        }
        return labels[decision]

    def _validate_proposal_base(
        self,
        proposal: ChapterPlanProposal,
    ) -> NovelProject:
        if proposal.status != "pending":
            raise ChapterPipelineError(
                f"候选计划状态为 {proposal.status}，不能继续执行"
            )
        project = self._store.load_project(proposal.book_id)
        if project.state.last_committed_chapter != proposal.base_chapter_number:
            raise ChapterPipelineError(
                "作品状态已变化，候选计划已过期；请重新规划下一章"
            )
        if proposal.chapter_number != project.state.last_committed_chapter + 1:
            raise ChapterPipelineError("候选计划章节号与当前作品状态不一致")
        return project

    @staticmethod
    def _effective_instruction(
        proposal: ChapterPlanProposal,
        *,
        additional_feedback: str | None = None,
    ) -> str | None:
        parts = []
        if proposal.user_instruction:
            parts.append(proposal.user_instruction)
        parts.extend(proposal.feedback_history)
        if additional_feedback:
            parts.append(additional_feedback)
        if not parts:
            return None
        return "\n".join(f"- {item}" for item in parts)

    @staticmethod
    def _should_revise(review: ReviewReport) -> bool:
        return not review.parse_failed and any(
            issue.severity == "critical" for issue in review.issues
        )

    @staticmethod
    def _strict_rejection_reason(review: ReviewReport) -> str | None:
        if review.parse_failed:
            return "最终审稿结果解析失败，strict 策略禁止写入正史"
        critical_issues = tuple(
            issue.description
            for issue in review.issues
            if issue.severity == "critical"
        )
        if critical_issues:
            return "最终审稿仍有 critical 问题：" + "；".join(critical_issues)
        return None
