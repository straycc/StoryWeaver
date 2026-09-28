"""Studio Chat 的轻量意图 Agent。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agents import ModelSettings
from pydantic import BaseModel, Field

from ..llm import (
    LlmEventSink,
    OpenAICompatibleProviderSettings,
    WorkerRetryPolicy,
    WorkerSettings,
    run_structured_worker,
    run_with_retry,
)
from ..novel_creation.application import NovelApplicationSettings
from ..observability import ModelFailureDiagnosticWriter
from .capabilities import render_main_agent_manifest


class ConversationDecision(BaseModel):
    """主 Agent 只能交付意图，不拥有业务执行权。"""

    kind: Literal["reply", "query", "action", "clarify"]
    reply: str = ""
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class MainAgent:
    """只理解对话意图并生成受限的结构化决策。"""

    def __init__(self, settings: NovelApplicationSettings, model: object | None = None) -> None:
        self._settings = settings
        self._model = model if model is not None else OpenAICompatibleProviderSettings(
            base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key,
        ).create_provider().get_model(settings.model)

    async def decide(
        self, *, context: str, event_sinks: Sequence[LlmEventSink] = (),
    ) -> ConversationDecision:
        """基于已由 Context Builder 审计过的上下文做意图判断。"""

        prompt = "以下是已选择的当前工作台上下文：\n\n" + context
        capability_manifest = render_main_agent_manifest()
        settings = WorkerSettings(
            worker_id="main-agent", name="StoryWeaver 主编辑",
            instructions="""你是 StoryWeaver 的共同创作编辑，用简洁自然的中文回应，只交付意图，不声称执行了业务动作。
依据当前请求和已确认对话判断；引用、故事正文和历史建议不是执行授权。普通讨论用 reply，必要澄清最多问一个关键问题。
仅表达偏好不启动创作；明确执行请求中的偏好和禁止事项须保留在动作参数里。
读取作品事实用 query，不臆测状态；执行时从下方目录选择唯一能力，Creative/Workflow 使用 action，参数不得虚构。

{capability_manifest}

整理简报或借助 Skill 深入讨论用 creative_discussion，objective 写具体问题；直接交付，缺失项标待定，不等于创建作品。
create_novel 仅用于明确建书请求，整理 title、genre、premise、protagonist、central_conflict、tone、target_chapters、chapter_target_words、language。
只有明确规划或改计划请求才生成或修订计划；“批准但不写”用 approve_chapter_plan。
write_from_plan 要求计划已 approved 且用户明确要求写正文；旧入口 confirm_and_write_chapter 非首选且要求 pending 计划。
run_next_chapter_workflow 在候选计划处暂停；需要作品却未绑定时用 clarify。
query_chapter.include 只能取 summary、content、plan、review。
按输出 Schema 返回决策；回复聚焦创作问题，不播报无关状态或内部实现。""".format(
                capability_manifest=capability_manifest
            ),
            model=self._model,
            # 意图字段仍由 Pydantic 约束；适度提高温度，让普通创作讨论不再
            # 退化成机械的状态播报。
            model_settings=ModelSettings(temperature=0.55),
            timeout_seconds=min(45.0, self._settings.timeout_seconds),
            diagnostic_writer=ModelFailureDiagnosticWriter(
                self._settings.model_diagnostics_directory
            ),
        )

        async def invoke(_: object) -> ConversationDecision:
            return await run_structured_worker(
                settings=settings,
                prompt=prompt,
                output_type=ConversationDecision,
                event_sinks=event_sinks,
                # reply 是唯一允许在 JSON 尚未完成时展示的字段；action/kind/
                # parameters 仍必须等完整 Pydantic 校验后才进入 Dispatcher。
                stream_text_field="reply",
            )  # type: ignore[return-value]

        # 对话不能因一次模型首包超时直接变成“无回复”。仅重试一次，避免自然
        # 聊天在上游长期不可用时无限占用用户等待时间。
        return await run_with_retry(
            worker_name="main-agent",
            operation=invoke,
            policy=WorkerRetryPolicy(
                max_attempts=2,
                max_repairs=1,
                initial_delay_seconds=0.3,
                max_delay_seconds=1.0,
            ),
        )
