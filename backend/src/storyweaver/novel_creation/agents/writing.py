"""统一承载章节初稿与修订模式的 Writing Agent。"""

from __future__ import annotations

from dataclasses import replace
from math import ceil
from typing import Any

from ...context import (
    AgentContextPolicy,
    ContextCandidate,
    default_agent_context_policies,
    require_within_budget,
    select_context,
)
from ...llm import LlmEventSink, WorkerRetryPolicy, WorkerSettings
from ...observability import get_log_context
from ...skills import CreativeTaskContext, SkillMaterializer
from ..context_renderer import ChapterContextRenderer
from ..exceptions import (
    ChapterDraftValidationError,
    ChapterReviewError,
    SerializationError,
)
from ..models import ChapterContext, ChapterDraft, ReviewIssue, ReviewReport
from ..serialization import (
    decode_chapter_draft,
    dumps_json,
    loads_json,
    to_data,
)
from ..validation import ChapterDraftValidator
from .base import BaseNovelAgent


WRITER_SYSTEM_PROMPT = """你是 StoryWeaver 的小说正文作者。
你只能使用用户消息中提供的有限上下文创作单章正文。

约束：
1. 严格遵守 chapter_plan、book_constraints、writing_rules 和 user_instruction。
2. 保持角色设定、知识边界、当前状态、事实和伏笔连续。
3. required_beats 必须在本章正文中发生，forbidden_events 不得发生。
4. 不得声称知道未被上下文提供的既定事实。
5. ending_hook 应在章节结尾形成自然悬念，不要输出计划说明或创作分析。
6. 标题应概括本章的核心意象、冲突或悬念；中文标题建议 2～12 个字。
7. 禁止使用“第N章”“章节N”“未命名章节”“正文”等占位标题，标题不得与书名相同。
8. 只返回一个 JSON 对象，不返回 Markdown 或 JSON Schema。

JSON 必须包含：chapter_number, title, content。
content 是完整章节正文字符串；字数由系统根据正文确定性计算。
"""


REVISER_SYSTEM_PROMPT = """你是 StoryWeaver 的小说章节修订者。
你只能根据结构化审查问题修订正文，不得改变章节计划或引入上下文外的既定事实。
保留原稿中没有问题的部分，优先修复 critical 问题。
标题应概括本章的核心意象、冲突或悬念；中文标题建议 2～12 个字。
禁止使用“第N章”“章节N”“未命名章节”“正文”等占位标题，标题不得与书名相同。

只返回 JSON 对象，包含 chapter_number、title、content。
不要返回解释、Markdown 或修订对照；字数由系统重新计算。
"""


class WritingAgent(BaseNovelAgent[dict[str, Any]]):
    """以写作、修订两种模式生成正文，并共享确定性校验能力。"""

    def __init__(
        self,
        *,
        renderer: ChapterContextRenderer | None = None,
        validator: ChapterDraftValidator | None = None,
        retry_policy: WorkerRetryPolicy | None = None,
        writer_settings: WorkerSettings | None = None,
        reviser_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
        writer_context_policy: AgentContextPolicy | None = None,
        reviser_context_policy: AgentContextPolicy | None = None,
        context_snapshot_sink: object | None = None,
        skill_materializer: SkillMaterializer | None = None,
    ) -> None:
        if reviser_settings is None:
            raise ValueError("Writing Agent 必须提供修订模式的 SDK WorkerSettings")
        super().__init__(
            agent_id="novel-writer",
            name="小说写作 Agent",
            system_prompt=WRITER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=writer_settings,
            event_sinks=event_sinks,
            skill_materializer=skill_materializer,
        )
        policies = default_agent_context_policies()
        self._renderer = renderer or ChapterContextRenderer()
        self._validator = validator or ChapterDraftValidator()
        self._writer_context_policy = writer_context_policy or policies["writer"]
        self._reviser_context_policy = reviser_context_policy or policies["reviser"]
        self._reviser_settings = reviser_settings
        self._context_snapshot_sink = context_snapshot_sink

    async def write(
        self,
        context: ChapterContext,
        creative_task: CreativeTaskContext | None = None,
    ) -> ChapterDraft:
        """使用写作模式，根据有限上下文生成章节初稿。"""

        budget = self._writer_context_policy.budget.initial_dynamic_context
        if context.estimated_tokens > budget:
            raise ValueError(
                "Writer 初始动态上下文超过 Policy 预算："
                f"{context.estimated_tokens}/{budget} Token"
            )

        output_limit = self._output_token_limit(context)
        active_settings = replace(
            self._sdk_settings,
            model_settings=replace(
                self._sdk_settings.model_settings,
                max_tokens=output_limit,
            ),
        )
        prompt = await self._with_skills(
            self._renderer.render(context),
            creative_task=creative_task,
            objective="根据已确认计划创作完整章节正文",
            total_token_budget=budget,
        )
        return await self._generate_validated(
            prompt,
            lambda raw: self._convert_draft(
                raw,
                context=context,
                error_type=ChapterDraftValidationError,
                error_prefix="Writer 输出无法转换为章节正文",
            ),
            repair_instruction=(
                "上一次章节正文未通过校验。请使用完全相同的章节计划和上下文重写，"
                "针对校验错误修正 JSON、章节号、标题或正文长度，不要改变本章目标。"
            ),
            sdk_settings=active_settings,
            agent_id="novel-writer",
        )

    async def revise(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        review: ReviewReport,
        creative_task: CreativeTaskContext | None = None,
    ) -> ChapterDraft:
        """使用修订模式，只处理审查报告中的可执行问题。"""

        actionable_issues = self._actionable_issues(review)
        prompt = self._revision_prompt(
            context=context,
            draft=draft,
            actionable_issues=actionable_issues,
        )
        prompt = await self._with_skills(
            prompt,
            creative_task=creative_task,
            objective="根据审查问题修订正文并保持创作要求",
            total_token_budget=(
                self._reviser_context_policy.budget.initial_dynamic_context
            ),
        )
        require_within_budget(
            prompt,
            budget=self._reviser_context_policy.budget.initial_dynamic_context,
            label="Reviser 初始上下文",
        )
        try:
            return await self._generate_validated(
                prompt,
                lambda raw: self._convert_draft(
                    raw,
                    context=context,
                    error_type=ChapterReviewError,
                    error_prefix="Reviser 输出无法转换为章节正文",
                ),
                repair_instruction=(
                    "上一次修订稿未通过校验。请继续遵守原章节计划和审查意见，"
                    "只修正失败项并返回完整 ChapterDraft JSON。"
                ),
                sdk_settings=self._reviser_settings,
                agent_id="novel-reviser",
            )
        finally:
            self._record_revision_context_snapshot(context=context, prompt=prompt)

    def _actionable_issues(self, review: ReviewReport) -> tuple[ReviewIssue, ...]:
        if review.parse_failed:
            raise ChapterReviewError("不能根据解析失败的审查报告修订正文")
        if not review.issues:
            raise ChapterReviewError("审查报告没有可执行的修订问题")
        return tuple(
            issue for issue in review.issues if issue.severity != "info"
        ) or review.issues

    def _revision_prompt(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        actionable_issues: tuple[ReviewIssue, ...],
    ) -> str:
        related_source_ids = {
            source_id
            for issue in actionable_issues
            for source_id in issue.related_source_ids
        }
        always_include = {
            "chapter_plan",
            "book_constraints",
            "writing_rules",
            "user_instruction",
            "creative_control",
        }
        revision_entries = tuple(
            entry
            for entry in context.entries
            if entry.source_type in always_include
            or entry.source_id in related_source_ids
        )
        revision_context = ChapterContext(
            chapter_number=context.chapter_number,
            entries=revision_entries,
            estimated_tokens=sum(
                max(1, (len(item.content) + 1) // 2) + 8
                for item in revision_entries
            ),
            book_id=context.book_id,
        )
        return (
            "请根据审查报告修订以下章节。\n\n"
            + self._renderer.render_entries(revision_context)
            + "\n\n## 原稿\n"
            + dumps_json(to_data(draft))
            + "\n## 可执行审查问题\n"
            + dumps_json(to_data(actionable_issues))
        )

    def _convert_draft(
        self,
        raw_draft: dict[str, Any],
        *,
        context: ChapterContext,
        error_type: type[ChapterDraftValidationError] | type[ChapterReviewError],
        error_prefix: str,
    ) -> ChapterDraft:
        normalized_draft = self._with_computed_word_count(raw_draft)
        try:
            draft = decode_chapter_draft(normalized_draft)
        except SerializationError as exc:
            raise error_type(f"{error_prefix}：{exc}") from exc
        return self._validator.validate_and_normalize(
            context=context,
            draft=draft,
        )

    def _output_token_limit(self, context: ChapterContext) -> int:
        """按目标字数计算初稿输出额度，并受写作模式预算约束。"""

        target_words = 0
        constraints = next(
            (
                item
                for item in context.entries
                if item.source_type == "book_constraints"
            ),
            None,
        )
        if constraints is not None:
            value = loads_json(constraints.content)
            if isinstance(value, dict):
                raw_target = value.get("chapter_target_words")
                if isinstance(raw_target, int) and not isinstance(raw_target, bool):
                    target_words = raw_target
        estimated = ceil(target_words * 1.8 * 1.5) + 1_024
        return min(
            self._writer_context_policy.budget.output_reserve,
            max(6_144, estimated),
        )

    def _record_revision_context_snapshot(
        self,
        *,
        context: ChapterContext,
        prompt: str,
    ) -> None:
        """保存修订模式的隔离输入；观测失败不改变修订结果。"""

        sink = self._context_snapshot_sink
        if sink is None:
            return
        try:
            book_version = context.chapter_number - 1
            candidate = ContextCandidate(
                source_id=f"reviser:chapter:{context.chapter_number}",
                source_type="reviser_initial_context",
                content=prompt,
                reason="原稿、可执行审查问题及其引用的硬约束",
                protected=True,
                priority=100,
            )
            _, trace = select_context(
                agent_role="reviser",
                policy_version=self._reviser_context_policy.policy_version,
                book_version=book_version,
                token_budget=(
                    self._reviser_context_policy.budget.initial_dynamic_context
                ),
                candidates=(candidate,),
            )
            sink.save(
                agent_role="reviser",
                book_id=context.book_id,
                book_version=book_version,
                policy_version=trace.policy_version,
                renderer_version=trace.renderer_version,
                rendered_context=prompt,
                trace=trace.to_data(),
                job_id=get_log_context().get("run_id"),
            )
        except Exception:
            return

    def _with_computed_word_count(
        self,
        raw_draft: dict[str, Any],
    ) -> dict[str, Any]:
        """模型不负责字数；由正文内容确定性计算。"""

        normalized = dict(raw_draft)
        content = normalized.get("content")
        if isinstance(content, str):
            normalized["word_count"] = max(
                1,
                self._validator.count_text_units(content),
            )
        return normalized
