"""根据结构化审查问题执行单次修订的 Reviser Agent。"""

from __future__ import annotations

from typing import Any

from ...llm import LlmEventSink, WorkerRetryPolicy, WorkerSettings
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
        prompt = (
            "请根据审查报告修订以下章节。\n\n"
            + self._renderer.render_entries(context)
            + "\n\n## 原稿\n"
            + dumps_json(to_data(draft))
            + "\n## 审查报告\n"
            + dumps_json(to_data(review))
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

        return await self._generate_validated(
            prompt,
            convert,
            repair_instruction=(
                "上一次修订稿未通过校验。请继续遵守原章节计划和审查意见，"
                "只修正失败项并返回完整 ChapterDraft JSON。"
            ),
        )

    def _with_computed_word_count(self, raw_draft: dict[str, Any]) -> dict[str, Any]:
        """模型输出不含冗余字数，由正文内容确定性计算。"""

        normalized = dict(raw_draft)
        content = normalized.get("content")
        if isinstance(content, str):
            normalized["word_count"] = max(1, self._validator.count_text_units(content))
        return normalized
