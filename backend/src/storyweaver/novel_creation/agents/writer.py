"""消费有限 ChapterContext 并生成章节正文的 Writer Agent。"""

from __future__ import annotations

from typing import Any

from ...llm import LlmEventSink, WorkerRetryPolicy, WorkerSettings
from ..context_renderer import ChapterContextRenderer
from ..exceptions import ChapterDraftValidationError, SerializationError
from ..models import ChapterContext, ChapterDraft
from ..serialization import decode_chapter_draft
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


class WriterAgent(BaseNovelAgent[dict[str, Any]]):
    """通过通用 Agent Runtime 生成并确定性校验章节正文。"""

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
            agent_id="novel-writer",
            name="小说正文作者",
            system_prompt=WRITER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
        )
        self._renderer = renderer or ChapterContextRenderer()
        self._validator = validator or ChapterDraftValidator()

    async def write(self, context: ChapterContext) -> ChapterDraft:
        def convert(raw_draft: dict[str, Any]) -> ChapterDraft:
            normalized_draft = self._with_computed_word_count(raw_draft)
            try:
                draft = decode_chapter_draft(normalized_draft)
            except SerializationError as exc:
                raise ChapterDraftValidationError(
                    f"Writer 输出无法转换为章节正文：{exc}"
                ) from exc
            return self._validator.validate_and_normalize(
                context=context,
                draft=draft,
            )

        return await self._generate_validated(
            self._renderer.render(context),
            convert,
            repair_instruction=(
                "上一次章节正文未通过校验。请使用完全相同的章节计划和上下文重写，"
                "针对校验错误修正 JSON、章节号、标题或正文长度，不要改变本章目标。"
            ),
        )

    def _with_computed_word_count(self, raw_draft: dict[str, Any]) -> dict[str, Any]:
        """模型不负责字数；在进入领域模型前由正文内容确定性计算。"""

        normalized = dict(raw_draft)
        content = normalized.get("content")
        if isinstance(content, str):
            normalized["word_count"] = max(1, self._validator.count_text_units(content))
        return normalized
