"""Studio Chat 的轻量意图 Agent。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from agents import ModelSettings
from pydantic import BaseModel, Field

from ..llm import OpenAICompatibleProviderSettings, WorkerSettings, run_structured_worker
from ..novel_creation.application import NovelApplicationSettings
from ..observability import ModelFailureDiagnosticWriter


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

    async def decide(self, *, context: str) -> ConversationDecision:
        """基于已由 Context Builder 审计过的上下文做意图判断。"""

        prompt = "以下是已选择的当前工作台上下文：\n\n" + context
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
只有用户明确要求“生成计划/规划下一章/改计划”时，才使用 prepare_chapter_plan 或 revise_chapter_plan。
只有用户明确要求“确认计划并写/按计划写/开始写正文”时，才使用 confirm_and_write_chapter。
“我希望”“保持”“不要揭穿”“偏向某种风格”等表达是创作讨论或偏好，必须使用 reply，
不能擅自生成计划、更不能确认写作。若没有 pending 章节计划，绝不能选择 confirm_and_write_chapter。
如果未绑定作品却需要作品操作，返回 clarify。普通讨论使用 reply，给出简洁、有帮助的中文答复。""",
                model=self._model,
                model_settings=ModelSettings(temperature=0.2),
                timeout_seconds=min(45.0, self._settings.timeout_seconds),
                diagnostic_writer=ModelFailureDiagnosticWriter(
                    self._settings.model_diagnostics_directory
                ),
            ),
            prompt=prompt,
            output_type=ConversationDecision,
        )  # type: ignore[return-value]
