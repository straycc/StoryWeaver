"""统一承载章节初稿与修订模式的 Writing Agent。"""

from __future__ import annotations

from dataclasses import replace
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
    to_data,
)
from ..validation import ChapterDraftValidator
from .base import BaseNovelAgent


WRITER_SYSTEM_PROMPT = """你是 StoryWeaver 的小说正文作者，根据提供的资料创作完整单章正文。
遵守计划、用户要求、写作规则和目标篇幅；落实必要情节点，避开禁止事件。
通过场景、行动和有区分度的对话展开故事，保持人物动机、因果与节奏，结尾自然承接计划中的悬念。
允许补充合理细节，不改变既定事实、人物能力或知识边界；避免梗概式叙述、重复解释和堆砌修辞。
标题应简短具体，不使用章节编号或占位标题，不与书名相同。
按提供的 Schema 返回正文，不附创作说明；字数由系统计算。"""


REVISER_SYSTEM_PROMPT = """你是 StoryWeaver 的章节修订者，根据审查证据优先修复 critical 问题。
只做必要修改，保留无问题内容、人物声音和叙述视角；检查修改后的因果与段落衔接。
不得改变计划、删除必要情节点或编造既定事实来规避问题。
标题保持简短具体，不用章节编号、占位标题或书名。
按提供的 Schema 返回完整修订稿，不返回片段、说明或修订对照。"""


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
        """为 high-thinking 写作保留完整输出额度，不再按正文字数压缩推理空间。"""

        del context
        return self._writer_context_policy.budget.output_reserve

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
