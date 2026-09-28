"""根据小说项目当前状态规划下一章的 Planner Agent。"""

from __future__ import annotations

from typing import Any, Mapping

from ...context import (
    AgentContextPolicy,
    ContextCandidate,
    default_agent_context_policies,
    require_within_budget,
    select_context,
    with_tool_results,
)
from ...observability import get_log_context
from ...llm import (
    LlmEvent,
    LlmEventSink,
    LlmEventType,
    NOVEL_OUTPUT_TYPES,
    WorkerRetryPolicy,
    WorkerSettings,
    run_research_then_submit,
)
from ...skills import CreativeTaskContext, SkillMaterializer
from ..exceptions import ChapterPlanValidationError, SerializationError
from ..hook_manager import HookManager
from ..models import BatchPlanningContext, ChapterPlan, NovelProject
from ..repository import StoryProjectRepository
from ..review_tools import (
    ReviewSnapshot,
    build_sdk_read_tools,
)
from ..serialization import decode_chapter_plan, dumps_json, to_data
from ..validation import ChapterPlanValidator
from .base import BaseNovelAgent


PLANNER_SYSTEM_PROMPT = """你是 StoryWeaver 的章节规划师，只规划输入 next_chapter_number 指定的下一章。
人物行动应符合动机、处境和知识边界；关键事件有因果联系，带来明确变化，规模适合目标篇幅。
遵守用户要求、当前大纲、批次进度与伏笔治理建议，不提前收束或重新推进已解决伏笔。
缺少关键依据时按需检索，不重复查询；未命中不代表事实不存在，不编造既定事实。
只引用资料或检索确认的角色与伏笔；推进和回收的伏笔同时列入 relevant_hook_ids。
按当前阶段要求检索或交付，输出结构遵守提供的 Schema。"""


class PlannerAgent(BaseNovelAgent[dict[str, Any]]):
    """生成下一章计划并执行项目级引用校验。"""

    def __init__(
        self,
        *,
        validator: ChapterPlanValidator | None = None,
        retry_policy: WorkerRetryPolicy | None = None,
        hook_manager: HookManager | None = None,
        store: StoryProjectRepository | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
        context_snapshot_sink: object | None = None,
        context_policy: AgentContextPolicy | None = None,
        skill_materializer: SkillMaterializer | None = None,
    ) -> None:
        super().__init__(
            agent_id="novel-planner",
            name="章节规划师",
            system_prompt=PLANNER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
            skill_materializer=skill_materializer,
        )
        self._validator = validator or ChapterPlanValidator()
        self._hook_manager = hook_manager or HookManager()
        self._store = store
        self._context_snapshot_sink = context_snapshot_sink
        self._context_policy = context_policy or default_agent_context_policies()["planner"]

    async def plan(
        self,
        *,
        project: NovelProject,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
        creative_task: CreativeTaskContext | None = None,
    ) -> ChapterPlan:
        if user_instruction is not None and not user_instruction.strip():
            raise ValueError("user_instruction 必须为非空字符串或 None")
        def convert(raw_plan: dict[str, Any]) -> ChapterPlan:
            hook_plan = raw_plan.get("hook_plan")
            required_hook_plan_fields = {
                "advance_hook_ids",
                "resolve_hook_ids",
                "new_hook_budget",
            }
            if not isinstance(hook_plan, dict) or (
                required_hook_plan_fields - set(hook_plan)
            ):
                raise ChapterPlanValidationError(
                    "Planner 输出必须包含完整 hook_plan：advance_hook_ids、"
                    "resolve_hook_ids、new_hook_budget"
                )
            try:
                plan = decode_chapter_plan(raw_plan)
            except SerializationError as exc:
                raise ChapterPlanValidationError(
                    f"Planner 输出无法转换为章节计划：{exc}"
                ) from exc
            self._validator.validate(project=project, plan=plan)
            return plan

        if self._store is None:
            raise ValueError("Planner 必须注入 StoryProjectRepository")
        snapshot = self._load_snapshot(project)
        prompt = self._tool_prompt(snapshot=snapshot, user_instruction=user_instruction, batch_context=batch_context)
        prompt = await self._with_skills(
            prompt,
            creative_task=creative_task,
            objective="规划当前创作任务的叙事推进、场景节拍和伏笔安排",
            total_token_budget=self._context_policy.budget.initial_dynamic_context,
        )
        require_within_budget(
            prompt,
            budget=self._context_policy.budget.initial_dynamic_context,
            label="Planner 初始上下文",
        )
        return await self._run_sdk_plan(prompt, snapshot, convert)

    async def _run_sdk_plan(self, prompt: str, snapshot: ReviewSnapshot, convert: Any) -> ChapterPlan:
        """SDK 两阶段规划；报告修复不重新运行研究工具。"""
        evidence: list[dict[str, object]] = []
        retrieval = self._context_policy.retrieval

        async def completed(name: str, index: int, succeeded: bool, exhausted: bool, elapsed: float, error: str | None, deduplicated: bool, arguments: Mapping[str, object]) -> None:
            event = LlmEvent(LlmEventType.TOOL_COMPLETED, "novel-planner", {
                "tool_name": name, "tool_call_index": index, "tool_call_limit": retrieval.max_tool_calls,
                "succeeded": succeeded, "budget_exhausted": exhausted, "elapsed_seconds": elapsed,
                "error": error, "deduplicated": deduplicated, "tool_arguments": dict(arguments),
            })
            for sink in self._event_sinks:
                await sink.on_event(event)

        tools = build_sdk_read_tools(
            snapshot=snapshot,
            max_tool_calls=retrieval.max_tool_calls,
            on_completed=completed,
            evidence=evidence,
            timeout_seconds=retrieval.tool_timeout_seconds,
            result_token_limit=retrieval.per_tool_result_tokens,
            transient_attempts=retrieval.transient_attempts,
        )

        try:
            output = await run_research_then_submit(
                settings=self._sdk_settings, prompt=prompt, read_tools=tools,
                output_type=NOVEL_OUTPUT_TYPES["novel-planner"], submit_tool_name="submit_plan",
                submit_description="提交最终章节计划；不会确认计划或修改正史。",
                max_research_turns=retrieval.max_research_turns,
                evidence=evidence,
                evidence_token_budget=retrieval.evidence_package_tokens,
                report_retry_policy=self._sdk_retry_policy,
                output_validator=lambda value: convert(value.model_dump()),
                event_sinks=self._event_sinks,
                tracing_enabled=True,
            )
            if not isinstance(output, ChapterPlan):
                raise TypeError("Planner Report 必须返回 ChapterPlan")
            return output
        finally:
            self._record_context_snapshot(prompt=prompt, snapshot=snapshot, evidence=evidence)

    def _record_context_snapshot(
        self,
        *,
        prompt: str,
        snapshot: ReviewSnapshot,
        evidence: list[dict[str, object]],
    ) -> None:
        """保存 Planner 初始索引与检索证据；失败审计不影响规划结果。"""

        sink = self._context_snapshot_sink
        if sink is None:
            return
        try:
            book_version = self._book_version(snapshot.project)
            candidate = ContextCandidate(
                source_id="planner:initial-prompt",
                source_type="planner_initial_context",
                content=prompt,
                reason="规划师可见的初始索引与用户指令",
                protected=True,
                priority=100,
            )
            _, trace = select_context(
                agent_role="planner",
                policy_version=self._context_policy.policy_version,
                book_version=book_version,
                token_budget=self._context_policy.budget.initial_dynamic_context,
                candidates=(candidate,),
                notes=("工具证据由 research 阶段追加",),
            )
            trace = with_tool_results(trace, evidence=evidence)
            sink.save(
                agent_role="planner",
                book_id=snapshot.project.metadata.book_id,
                book_version=book_version,
                policy_version=trace.policy_version,
                renderer_version=trace.renderer_version,
                rendered_context=prompt,
                trace=trace.to_data(),
                job_id=get_log_context().get("run_id"),
            )
        except Exception:
            # Snapshot 是观测能力，不得改变 Planner 的恢复语义。
            return

    def _book_version(self, project: NovelProject) -> int:
        loader = getattr(self._store, "load_project_with_version", None)
        if callable(loader):
            return int(loader(project.metadata.book_id)[1])
        return project.state.last_committed_chapter

    def _load_snapshot(self, project: NovelProject) -> ReviewSnapshot:
        """把本次规划固定在同一份已提交正史上。"""

        assert self._store is not None
        stored = self._store.load_project(project.metadata.book_id)
        if stored.state.last_committed_chapter != project.state.last_committed_chapter:
            raise ValueError("Planner 项目状态已过期，无法建立工具快照")
        return ReviewSnapshot(
            project=stored,
            chapter_summaries=self._store.load_chapter_summaries(stored.metadata.book_id),
        )

    def _tool_prompt(
        self,
        *,
        snapshot: ReviewSnapshot,
        user_instruction: str | None,
        batch_context: BatchPlanningContext | None = None,
    ) -> str:
        """为工具型 Planner 组装最小保护层和可检索索引。"""

        project = snapshot.project
        next_chapter = project.state.last_committed_chapter + 1
        active_outline = tuple(
            node
            for node in project.foundation.outline
            if node.chapter_start <= next_chapter <= node.chapter_end
        )
        fact_index = [
            {
                "fact_id": item.fact_id,
                "subject_id": item.subject_id,
                "predicate": item.predicate,
                "source_chapter": item.source_chapter,
                "importance": item.importance,
            }
            for item in sorted(
                project.state.current_facts,
                key=lambda item: (-item.importance, -item.source_chapter, item.fact_id),
            )[:5]
        ]
        payload = {
            "next_chapter_number": next_chapter,
            "book_constraints": {
                "title": project.metadata.title,
                "genre": project.metadata.genre,
                "target_chapters": project.metadata.target_chapters,
                "chapter_target_words": project.metadata.chapter_target_words,
            },
            "active_outline": active_outline,
            "current_story_position": {
                "last_committed_chapter": project.state.last_committed_chapter,
                "current_time": project.state.current_time,
                "current_location": project.state.current_location,
            },
            "character_index": [
                {"character_id": item.character_id, "name": item.name}
                for item in project.foundation.characters
            ],
            "high_importance_fact_index": fact_index,
            "hook_governance": self._hook_manager.planning_guidance(project=project),
            "user_instruction": user_instruction if batch_context is None else None,
            "batch_execution": self._batch_execution_data(batch_context),
        }
        return (
            "请规划下一章。下方是最小保护层和索引；只有实际需要时再调用只读工具，"
            "不要为凑工具次数而检索。\n\n## 最小规划上下文\n"
            + dumps_json(to_data(payload))
        )

    @staticmethod
    def _batch_execution_data(
        batch_context: BatchPlanningContext | None,
    ) -> dict[str, Any] | None:
        if batch_context is None:
            return None
        return {
            "start_chapter": batch_context.start_chapter,
            "end_chapter": batch_context.end_chapter,
            "current_chapter": batch_context.current_chapter,
            "remaining_chapters": batch_context.remaining_chapters,
            "is_final_chapter": batch_context.is_final_chapter,
            "overall_instruction": batch_context.overall_instruction,
            "current_chapter_rule": (
                "这是批次最后一章：完成整体要求的阶段性收束，同时保留下一卷钩子。"
                if batch_context.is_final_chapter
                else "这不是批次最后一章：只推进整体要求，不得提前完成阶段性收尾或主谜团揭晓。"
            ),
        }
