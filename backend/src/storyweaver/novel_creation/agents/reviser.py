"""根据结构化审查问题执行单次修订的 Reviser Agent。"""

from __future__ import annotations

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
from ..context_renderer import ChapterContextRenderer
from ..exceptions import ChapterReviewError, SerializationError
from ..models import ChapterContext, ChapterDraft, ReviewReport
from ..serialization import decode_chapter_draft, dumps_json, to_data
from ..validation import ChapterDraftValidator
from .base import BaseNovelAgent


REVISER_SYSTEM_PROMPT = """你是 StoryWeaver 的小说章节修订者。
你只能根据结构化审查问题修订正文，不得改变章节计划或引入上下文外的既定事实。
保留原稿中没有问题的部分，优先修复 critical 问题。
标题应概括本章的核心意象、冲突或悬念；中文标题建议 2～12 个字。
禁止使用“第N章”“章节N”“未命名章节”“正文”等占位标题，标题不得与书名相同。

只返回 JSON 对象，包含 chapter_number、title、content。
不要返回解释、Markdown 或修订对照；字数由系统重新计算。
"""


class ReviserAgent(BaseNovelAgent[dict[str, Any]]):
    """执行一次正文修订并重新应用确定性正文校验。"""

    def __init__(
        self,
        *,
        renderer: ChapterContextRenderer | None = None,
        validator: ChapterDraftValidator | None = None,
        retry_policy: WorkerRetryPolicy | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
        context_policy: AgentContextPolicy | None = None,
        context_snapshot_sink: object | None = None,
    ) -> None:
        super().__init__(
            agent_id="novel-reviser",
            name="章节修订者",
            system_prompt=REVISER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
        )
        self._renderer = renderer or ChapterContextRenderer()
        self._validator = validator or ChapterDraftValidator()
        self._context_policy = context_policy or default_agent_context_policies()["reviser"]
        self._context_snapshot_sink = context_snapshot_sink

    async def revise(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        review: ReviewReport,
    ) -> ChapterDraft:
        if review.parse_failed:
            raise ChapterReviewError("不能根据解析失败的审查报告修订正文")
        if not review.issues:
            raise ChapterReviewError("审查报告没有可执行的修订问题")
        actionable_issues = tuple(
            issue for issue in review.issues if issue.severity != "info"
        ) or review.issues
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
        rendered_context = self._renderer.render_entries(
            ChapterContext(
                chapter_number=context.chapter_number,
                entries=revision_entries,
                estimated_tokens=sum(max(1, (len(item.content) + 1) // 2) + 8 for item in revision_entries),
                book_id=context.book_id,
            )
        )
        prompt = (
            "请根据审查报告修订以下章节。\n\n"
            + rendered_context
            + "\n\n## 原稿\n"
            + dumps_json(to_data(draft))
            + "\n## 可执行审查问题\n"
            + dumps_json(to_data(actionable_issues))
        )
        require_within_budget(
            prompt,
            budget=self._context_policy.budget.initial_dynamic_context,
            label="Reviser 初始上下文",
        )

        def convert(raw_draft: dict[str, Any]) -> ChapterDraft:
            normalized_draft = self._with_computed_word_count(raw_draft)
            try:
                revised = decode_chapter_draft(normalized_draft)
            except SerializationError as exc:
                raise ChapterReviewError(
                    f"Reviser 输出无法转换为章节正文：{exc}"
                ) from exc
            return self._validator.validate_and_normalize(
                context=context,
                draft=revised,
            )

        try:
            return await self._generate_validated(
                prompt,
                convert,
                repair_instruction=(
                    "上一次修订稿未通过校验。请继续遵守原章节计划和审查意见，"
                    "只修正失败项并返回完整 ChapterDraft JSON。"
                ),
            )
        finally:
            self._record_context_snapshot(context=context, prompt=prompt)

    def _record_context_snapshot(
        self,
        *,
        context: ChapterContext,
        prompt: str,
    ) -> None:
        """保存 Reviser 的隔离输入；观测失败不改变修订结果。"""

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
                policy_version=self._context_policy.policy_version,
                book_version=book_version,
                token_budget=self._context_policy.budget.initial_dynamic_context,
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

    def _with_computed_word_count(self, raw_draft: dict[str, Any]) -> dict[str, Any]:
        """模型输出不含冗余字数，由正文内容确定性计算。"""

        normalized = dict(raw_draft)
        content = normalized.get("content")
        if isinstance(content, str):
            normalized["word_count"] = max(1, self._validator.count_text_units(content))
        return normalized
