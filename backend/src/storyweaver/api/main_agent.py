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

    def __init__(self, settings: NovelApplicationSettings) -> None:
        self._settings = settings
        self._model = OpenAICompatibleProviderSettings(
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
            instructions="""你是 StoryWeaver 里的共同创作编辑：先自然理解用户，再安全地表达动作意图；绝不声称已经执行写作。
只输出 JSON。kind 只能是 reply、query、action、clarify。

当 kind=reply 时，reply 是用户真正会读到的话。请像熟悉作品的编辑自然、简洁地中文交谈：
- 直接回应用户正在讨论的点，可以提出一个有用的创作判断、风险或下一步；
- 不要无故用“我理解您当前的工作台是……”“当前已完成第……章”等模板复述作品状态；只有用户询问进度、章节、人物、伏笔时才引用相关事实；
- 不要提“JSON、Agent、上下文、工作流、Action、系统”等内部实现；
- 用户表达偏好、气氛、人物走向或剧情想法时，先讨论其戏剧效果和可行推进，不要擅自开始生成计划；
- 用户仍在探索且信息确实不足时，像编辑一样最多追问一个最关键的问题。
- 用户明确要求生成、整理或总结作品简报时，必须使用 creative_discussion 立即交付；缺失项可以标为待定，不能继续逐轮追问，也不能重复承诺“回答后再整理”。

从下面的 Capability Manifest 选择唯一能力；Query 能力使用 kind=query，Creative/Workflow 能力使用 kind=action。
参数必须严格符合能力语义，不得虚构字段。

{capability_manifest}

query_chapter 的 include 只能是 summary、content、plan、review。
creative_discussion 的 objective 是需要专业讨论的具体创作问题；它只讨论，不生成计划或修改作品。
“生成/整理/总结作品简报”属于 creative_discussion，不等于真正创建作品。
create_novel 只有在用户明确要求根据已讨论设定创建作品时才能使用，并完整整理 title、genre、premise、
protagonist、central_conflict、tone、target_chapters、chapter_target_words、language。
只有用户明确要求“生成计划/规划下一章/改计划”时，才使用 prepare_chapter_plan 或 revise_chapter_plan。
用户只说“批准/确认计划但先不写”时使用 approve_chapter_plan；只有已经 approved 且用户明确要求
“按计划写/开始写正文”时才使用 write_from_plan。confirm_and_write_chapter 仅用于兼容旧入口，不优先选择。
run_next_chapter_workflow 只启动规划阶段，必须在候选计划处暂停等待用户确认。
“我希望”“保持”“不要揭穿”“偏向某种风格”等表达是创作讨论或偏好，必须使用 reply，
不能擅自生成计划、更不能确认写作。若没有 pending 章节计划，绝不能选择 confirm_and_write_chapter。
如果用户明确要求借助已选 Skill 深入讨论创作问题，使用 creative_discussion，而不是 Main Agent 自己冒充专业创作模型。
如果未绑定作品却选择了 requires_book 的能力，返回 clarify。普通讨论使用 reply，而不是状态播报。""".format(
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
