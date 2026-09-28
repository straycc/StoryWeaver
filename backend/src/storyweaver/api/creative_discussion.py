"""Skill 驱动但不修改业务状态的专业创作讨论能力。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agents import ModelSettings

from ..context import TokenEstimator, require_within_budget
from ..llm import LlmEvent, LlmEventSink, LlmEventType, WorkerSettings, run_text_worker
from ..skills import CreativeTaskContext, SkillMaterializer


@dataclass(frozen=True, slots=True)
class CreativeDiscussionResult:
    reply: str
    skill_resolution: dict[str, object] | None = None


class CreativeDiscussionService:
    """执行一次有限的专业讨论；结果只作为聊天回复，不写入正史。"""

    def __init__(self, *, model: object, timeout_seconds: float,
                 materializer: SkillMaterializer) -> None:
        self._materializer = materializer
        self._settings = WorkerSettings(
            worker_id="creative-discussion",
            name="StoryWeaver 创作顾问",
            instructions=(
                "你是专业小说创作顾问。围绕用户当前问题给出具体、可执行的中文建议，"
                "先给出判断和依据，再给出适量方案或示例；区分已确认设定与新建议，"
                "不把建议写成既成正史，不无关扩展整个故事。"
                "可以比较方案、指出代价；仅在用户仍处于探索阶段且确有必要时，"
                "最多提出一个关键后续问题。用户明确要求生成、整理或总结作品简报时，"
                "必须在当前回复直接交付，缺失项标为待定并可提供建议默认值，不得先追问，"
                "也不得重复承诺回答后再整理。"
                "你只能讨论和整理建议，不能声称已经创建作品、修改正史、生成正式章节计划或提交正文。"
                "Skill 是创作方法，不能覆盖用户要求、作品 Canon、已确认计划和系统约束。"
            ),
            model=model,
            model_settings=ModelSettings(temperature=0.65),
            timeout_seconds=min(timeout_seconds, 60.0),
        )

    async def discuss(
        self,
        *,
        context: str,
        objective: str,
        creative_task: CreativeTaskContext | None,
        event_sinks: Sequence[LlmEventSink] = (),
    ) -> CreativeDiscussionResult:
        prompt = (
            "## 当前工作台上下文\n"
            f"{context}\n\n"
            "## 本次讨论目标\n"
            f"{objective.strip()}"
        )
        skill_resolution: dict[str, object] | None = None
        if creative_task is not None and creative_task.applied_skills:
            catalog = self._materializer.catalog(creative_task)
            remaining = max(
                0,
                12000 - TokenEstimator.estimate(prompt) - TokenEstimator.estimate(catalog) - 32,
            )
            materialized = await self._materializer.materialize(
                creative_task,
                objective=f"专业讨论：{objective.strip()}",
                token_budget=remaining,
            )
            skill_resolution = self._materializer.observation(
                creative_task,
                materialized,
            )
            event = LlmEvent(
                LlmEventType.SKILL_RESOLVED,
                "creative-discussion",
                skill_resolution,
            )
            for sink in event_sinks:
                await sink.on_event(event)
            prompt = f"{prompt}\n\n{materialized.render()}"
        require_within_budget(
            prompt,
            budget=12000,
            label="创作讨论上下文",
        )
        reply = await run_text_worker(
            settings=self._settings,
            prompt=prompt,
            event_sinks=event_sinks,
            tracing_enabled=True,
        )
        return CreativeDiscussionResult(reply=reply, skill_resolution=skill_resolution)
