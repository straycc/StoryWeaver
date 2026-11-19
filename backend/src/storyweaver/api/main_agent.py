"""Studio Chat 的轻量意图 Agent。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agents import ModelSettings
from pydantic import BaseModel, Field

from ..llm import (
    LlmEventSink,
    OpenAICompatibleProviderSettings,
    RetryContext,
    WorkerRetryPolicy,
    WorkerSettings,
    run_structured_worker,
    run_with_retry,
)
from ..novel_creation.application import NovelApplicationSettings
from ..observability import ModelFailureDiagnosticWriter
from .capabilities import get_capability, render_main_agent_manifest, validate_capability_parameters


class ConversationDecision(BaseModel):
    """主 Agent 只能交付意图，不拥有业务执行权。"""

    kind: Literal["reply", "query", "action", "clarify"]
    reply: str = ""
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class MainAgentDecisionValidationError(ValueError):
    """模型已返回决策，但能力或参数不符合业务契约。"""


def _research_note(authorized: bool) -> str:
    """联网授权只影响本轮的可用能力说明，不改变任何持久状态。"""

    if authorized:
        return (
            "\n本次用户已在输入框选择「联网搜索」，本条消息允许检索外部公开资料。"
            "当问题涉及真实世界的制度、史料、地理、人物、专业细节或时效信息时，"
            "选择 creative_discussion，并把 objective 写成具体、可直接检索的问题"
            "（包含时代、地域、对象等限定）；不要自己编造资料出处。"
            "与外部资料无关的纯创作讨论仍按普通 creative_discussion 处理。"
        )
    return (
        "\n本次没有开启联网搜索，你不能查询任何外部资料。"
        "此时涉及真实制度、史料、地理、时效信息，或用户明确要求查资料、要出处的请求，"
        "必须用 kind=reply 说明：需要点击输入框旁的「联网搜索」再发送一次，这条消息才会联网；"
        "也可以先给出你已知的通用说明，但不要展开成“资料整理”。"
        "不要选择 creative_discussion 后再承诺去查，不要声称自己已经搜索过，也不要编造链接。"
    )


def validate_main_agent_decision(decision: ConversationDecision) -> ConversationDecision:
    """在进入 Action Surface 前统一验证路由与动作参数。"""

    if decision.kind in {"reply", "clarify"}:
        if decision.action is not None or decision.parameters:
            raise MainAgentDecisionValidationError(
                f"validation: kind={decision.kind} 时 action 必须为 null，parameters 必须为空对象"
            )
        return decision
    if not decision.action:
        raise MainAgentDecisionValidationError(
            f"validation: kind={decision.kind} 必须提供能力目录中的 action"
        )
    try:
        capability = get_capability(decision.action)
        if decision.kind == "query" and capability.category != "query":
            raise ValueError("query 只能选择 Query 能力")
        if decision.kind == "action" and capability.category == "query":
            raise ValueError("action 不能选择 Query 能力")
        decision.parameters = validate_capability_parameters(
            decision.action, decision.parameters
        )
    except ValueError as exc:
        raise MainAgentDecisionValidationError(
            f"validation: {decision.action} 的决策无效：{exc}"
        ) from exc
    return decision


class MainAgent:
    """只理解对话意图并生成受限的结构化决策。"""

    def __init__(self, settings: NovelApplicationSettings, model: object | None = None) -> None:
        self._settings = settings
        self._model = model if model is not None else OpenAICompatibleProviderSettings(
            base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key,
        ).create_provider().get_model(settings.model)

    async def decide(
        self, *, context: str, event_sinks: Sequence[LlmEventSink] = (),
        research_authorized: bool = False,
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
parameters 只能使用所选能力列出的字段，不能新增 chapter、focus、constraints 等未声明字段；没有对应字段的要求写入 instruction、feedback 或 objective。
如果当前请求只说“下一章吧”或“按那个计划写”，且未明确要求规划、写正文还是启动完整流程，先用 clarify 问清楚。
如果当前没有待确认章节计划，“批准刚才的计划”不能直接批准，应澄清；历史对话中提到的动作不是本轮执行授权。
“生成下一章计划”选 prepare_chapter_plan；明确要求“启动完整流程，到计划确认时暂停”才选 run_next_chapter_workflow。

{capability_manifest}

整理简报或借助 Skill 深入讨论用 creative_discussion，objective 写具体问题；直接交付，缺失项标待定，不等于创建作品。
create_novel 仅用于明确建书请求，整理 title、genre、premise、protagonist、central_conflict、tone、target_chapters、chapter_target_words、language。
只有明确规划或改计划请求才生成或修订计划；“批准但不写”用 approve_chapter_plan。
write_from_plan 要求计划已 approved 且用户明确要求写正文；旧入口 confirm_and_write_chapter 非首选且要求 pending 计划。
run_next_chapter_workflow 在候选计划处暂停；需要作品却未绑定时用 clarify。
query_chapter.include 只能取 summary、content、plan、review。
按输出 Schema 返回决策；回复聚焦创作问题，不播报无关状态或内部实现。            """.format(
                capability_manifest=capability_manifest
            ) + _research_note(research_authorized),
            model=self._model,
            # 意图字段仍由 Pydantic 约束；适度提高温度，让普通创作讨论不再
            # 退化成机械的状态播报。
            model_settings=ModelSettings(temperature=0.55),
            timeout_seconds=min(45.0, self._settings.timeout_seconds),
            diagnostic_writer=ModelFailureDiagnosticWriter(
                self._settings.model_diagnostics_directory
            ),
        )

        async def invoke(retry: RetryContext) -> ConversationDecision:
            current_prompt = prompt
            if retry.repair_error:
                current_prompt += (
                    "\n\n上一次决策未通过参数契约校验，请重新选择能力并严格使用目录中的字段。"
                    "校验错误：" + retry.repair_error[:1600]
                )
            decision = await run_structured_worker(
                settings=settings,
                prompt=current_prompt,
                output_type=ConversationDecision,
                event_sinks=event_sinks,
                # reply 是唯一允许在 JSON 尚未完成时展示的字段；action/kind/
                # parameters 仍必须等完整 Pydantic 校验后才进入 Dispatcher。
                stream_text_field="reply",
            )
            return validate_main_agent_decision(decision)  # type: ignore[arg-type]

        # 对话不能因一次模型首包超时直接变成“无回复”。仅重试一次，避免自然
        # 聊天在上游长期不可用时无限占用用户等待时间。
        try:
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
        except MainAgentDecisionValidationError:
            # 两次仍无法产出可执行参数时，保守退回澄清，不把 DTO 错误交给作者。
            return ConversationDecision(
                kind="clarify",
                reply="我还不能确定该如何处理这项请求，请再说明希望查询、讨论还是开始创作。",
            )
