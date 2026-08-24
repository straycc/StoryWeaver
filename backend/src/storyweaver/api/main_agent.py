"""Studio Chat 的轻量意图 Agent。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from agents import ModelSettings
from pydantic import BaseModel, Field

from ..llm import OpenAICompatibleProviderSettings, WorkerSettings, run_structured_worker
from ..novel_creation.application import NovelApplicationSettings


class ConversationDecision(BaseModel):
    """主 Agent 只能交付意图，不拥有业务执行权。"""

    kind: Literal["reply", "query", "action", "clarify"]
    reply: str = ""
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[dict[str, Any]] = Field(default_factory=list)


class MainAgent:
    """只理解对话意图并生成受限的结构化决策。"""

    def __init__(self, settings: NovelApplicationSettings) -> None:
        self._settings = settings
        self._model = OpenAICompatibleProviderSettings(
            base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key,
        ).create_provider().get_model(settings.model)

    async def decide(
        self, *, content: str, book_summary: str | None, recent_messages: list[Mapping[str, Any]],
        pending_actions: list[Mapping[str, Any]],
    ) -> ConversationDecision:
        prompt = (
            "用户消息：\n" + content + "\n\n"
            "当前作品摘要：\n" + (book_summary or "未绑定作品") + "\n\n"
            "最近对话：\n" + "\n".join(
                f"{item.get('role', 'user')}：{item.get('content', '')}" for item in recent_messages[-12:]
            ) + "\n\n待确认操作：\n" + repr(pending_actions[-3:])
        )
        return await run_structured_worker(
            settings=WorkerSettings(
                worker_id="main-agent", name="StoryWeaver 主编辑",
                instructions="""你负责理解用户在小说工作台中的意图，绝不声称已经执行写作。
只输出 JSON。kind 只能是 reply、query、action、clarify。
query 的 action 只能是 query_book_state、query_story_progress、query_completed_chapters、query_chapter、
query_pending_plan、query_recent_review、query_open_foreshadowings、query_character、query_foreshadowing、explain_review。
query_completed_chapters 可使用 limit；query_chapter 使用 chapter_number 和 include（summary、content、plan、review）；
query_recent_review 使用可选 chapter_number；query_open_foreshadowings 使用可选 limit；
query_character 与 query_foreshadowing 使用 query。
action 的 action 只能是 prepare_chapter_plan、revise_chapter_plan、confirm_and_write_chapter、rewrite_chapter。
prepare_chapter_plan 参数使用 instruction；revise_chapter_plan 使用 feedback 和可选 proposal_id；
confirm_and_write_chapter 使用可选 proposal_id；rewrite_chapter 使用 chapter_number 和可选 instruction。
suggestions 最多给出 3 个可点击后续动作，每项必须有 label、action、parameters；不确定时返回空数组。
如果未绑定作品却需要作品操作，返回 clarify。普通讨论使用 reply，给出简洁、有帮助的中文答复。""",
                model=self._model,
                model_settings=ModelSettings(temperature=0.2),
                timeout_seconds=min(45.0, self._settings.timeout_seconds),
            ),
            prompt=prompt,
            output_type=ConversationDecision,
        )  # type: ignore[return-value]
