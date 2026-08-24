"""根据创作简报设计小说基础资料的 Architect Agent。"""

from __future__ import annotations

from typing import Any

from ...llm import LlmEventSink, WorkerRetryPolicy, WorkerSettings
from ..exceptions import NovelFoundationValidationError, SerializationError
from ..models import CreateNovelRequest, NovelFoundation
from ..serialization import decode_novel_foundation, dumps_json, to_data
from ..validation import NovelFoundationValidator
from .base import BaseNovelAgent


ARCHITECT_SYSTEM_PROMPT = """你是 StoryWeaver 的小说架构师。
你的职责是把创作简报设计成可以供后续章节规划器使用的结构化基础资料。

约束：
1. 只依据创作简报进行合理扩展，不改写用户指定的主角和核心冲突。
2. 至少设计两个角色，且必须包含用户指定的主角。
3. 大纲节点的章节范围不能超过目标章节数。
4. 角色 ID、大纲节点 ID 和伏笔 ID 使用稳定、简短的英文小写连字符格式。
5. initial_hooks 中伏笔的 opened_chapter 和 last_advanced_chapter 都必须为 0。
6. 只返回一个 JSON 对象，不返回说明文字、Markdown 或 JSON Schema。
7. 顶层只返回下方列出的字段，不要重复返回创作简报中的 title、genre、
   protagonist、tone、target_chapters、chapter_target_words 或 language。

JSON 必须包含以下字段：
premise, world_setting, central_conflict, ending_direction,
characters, outline, writing_rules, initial_hooks。

characters 每项包含：character_id, name, role, personality（字符串数组）,
motivation, long_term_goal, conflict, speech_style,
knowledge_boundaries（字符串数组）。

outline 每项包含：node_id, title, chapter_start, chapter_end, goal,
expected_changes（字符串数组）。

initial_hooks 每项包含：hook_id, name, description, status, importance,
opened_chapter, last_advanced_chapter, expected_payoff。name 是供作者阅读的简短中文名称，
例如“雨中的泥脚印”；hook_id 仍使用英文小写连字符格式。
status 只能是 open、progressing、resolved 或 deferred，importance 为 1 到 5。
"""


# 这些字段属于用户创作简报或项目元数据。部分模型会在正确的 Foundation
# 顶层重复回显它们；它们不参与 Foundation 持久化，可以在模型边界安全剥离。
_ECHOED_REQUEST_FIELDS = frozenset(
    {
        "title",
        "genre",
        "protagonist",
        "tone",
        "target_chapters",
        "chapter_target_words",
        "language",
    }
)


class ArchitectAgent(BaseNovelAgent[dict[str, Any]]):
    """调用现有 Agent Runtime，并把模型 JSON 转为 ``NovelFoundation``。"""

    def __init__(
        self,
        *,
        validator: NovelFoundationValidator | None = None,
        retry_policy: WorkerRetryPolicy | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
    ) -> None:
        super().__init__(
            agent_id="novel-architect",
            name="小说架构师",
            system_prompt=ARCHITECT_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
        )
        self._validator = validator or NovelFoundationValidator()

    async def create(self, request: CreateNovelRequest) -> NovelFoundation:
        def convert(raw_foundation: dict[str, Any]) -> NovelFoundation:
            normalized = self._remove_echoed_request_fields(raw_foundation)
            try:
                foundation = decode_novel_foundation(normalized)
            except SerializationError as exc:
                raise NovelFoundationValidationError(
                    f"Architect 输出无法转换为小说基础资料：{exc}"
                ) from exc
            self._validator.validate(request=request, foundation=foundation)
            return foundation

        return await self._generate_validated(
            self._build_prompt(request),
            convert,
            repair_instruction=(
                "上一次小说基础资料未通过校验。请根据错误修正，"
                "不要改变用户简报，只返回完整 NovelFoundation JSON。"
            ),
        )

    @staticmethod
    def _remove_echoed_request_fields(
        raw_foundation: dict[str, Any],
    ) -> dict[str, Any]:
        """剥离模型重复回显的请求元数据，保留其他未知字段供严格校验。"""

        return {
            key: value
            for key, value in raw_foundation.items()
            if key not in _ECHOED_REQUEST_FIELDS
        }

    @staticmethod
    def _build_prompt(request: CreateNovelRequest) -> str:
        return (
            "请根据以下创作简报生成小说基础资料。所有输出内容使用简报指定语言。\n\n"
            + dumps_json(to_data(request))
        )
