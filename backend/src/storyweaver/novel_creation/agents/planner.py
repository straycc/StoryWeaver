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


PLANNER_SYSTEM_PROMPT = """你是 StoryWeaver 的章节规划师。
你的职责是根据小说基础资料、当前权威状态和用户本章指令，规划且只规划下一章。

约束：
1. chapter_number 必须等于输入中的 next_chapter_number。
2. participating_character_ids 和 relevant_hook_ids 只能引用输入中存在的 ID。
3. required_beats 必须至少有一项，并且不能与 forbidden_events 直接冲突。
4. 不得推进 status 为 resolved 的伏笔。
5. 计划应推进当前大纲节点，但不要提前解决结局或泄露角色未知信息。
6. 最小上下文只包含索引；人物稳定设定、完整事实、历史章节和全部伏笔请按需调用只读工具。
   没有检索到结果不代表可以编造；工具只读，不能修改正史。
7. 总工具调用预算为 4 次，模型最多运行 3 回合。工具预算耗尽后，必须根据已有资料直接输出 ChapterPlan。
8. hook_plan 必须包含 advance_hook_ids、resolve_hook_ids、new_hook_budget；两类 ID 都必须同步列入 relevant_hook_ids。
9. 只返回一个 JSON 对象，不返回说明文字、Markdown 或 JSON Schema。
10. 请优先从 hook_governance.priority_hooks 中选择 1 至 2 条未解决伏笔写入
   relevant_hook_ids，并让 required_beats 明确推进其中至少一条；不必每章新开谜团。
11. 若输入包含 batch_execution 且 is_final_chapter 为 false，overall_instruction 中的收尾、
   揭晓、回收或进入下一卷等要求都是最终章方向：本章只能铺垫或推进，绝不可提前完成批次级收尾。
   只有 is_final_chapter 为 true 时，才必须落实该整体要求中的收束目标。
12. 检索顺序：优先读取最近一个已提交章节摘要，再查询开放伏笔；只有计划确实涉及
   特定角色、物件或世界规则时，才查询实体证据或基础设定。不要对相同工具和相同参数
   重复查询；工具返回未命中或参数无效时，改用已有证据继续规划，不要反复重试。

JSON 必须包含：chapter_number, goal, participating_character_ids,
location, required_beats, forbidden_events, relevant_hook_ids,
ending_hook, style_focus, hook_plan。
所有 ID 列表及 beats、events、style_focus 都使用 JSON 字符串数组。
"""


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
    ) -> None:
        super().__init__(
            agent_id="novel-planner",
            name="章节规划师",
            system_prompt=PLANNER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
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
