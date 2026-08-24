"""审查章节计划遵循和连续性的 Reviewer Agent。"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ...context_management import ContextCandidate, source_ref, trace_from_candidates, with_tool_results
from ...observability import get_log_context
from ...llm import LlmEvent, LlmEventSink, LlmEventType, NOVEL_OUTPUT_TYPES, WorkerRetryPolicy, WorkerSettings, run_research_then_submit, run_with_retry
from ..context_renderer import ChapterContextRenderer
from ..exceptions import SerializationError
from ..models import ChapterContext, ChapterDraft, ChapterPlan, ReviewReport
from ..project_store import NovelProjectStore
from ..review_tools import (
    CanonEvidenceSearchTool,
    ChapterSummaryTool,
    EntityEvidenceTool,
    FoundationQueryTool,
    OpenForeshadowingsTool,
    ReviewSnapshot,
    build_sdk_read_tools,
)
from ..serialization import (
    decode_chapter_plan,
    decode_review_report,
    dumps_json,
    loads_json,
    to_data,
)
from .base import BaseNovelAgent


REVIEWER_SYSTEM_PROMPT = """你是 StoryWeaver 的小说章节审查员。
请根据有限上下文审查正文，不要改写正文。

审查类别只能使用：plan_following、character_consistency、knowledge_boundary、
world_continuity、hook_consistency、structure、style。
严重程度只能使用 info、warning、critical。

严重程度判定规则：
1. critical：违反 forbidden_events、缺失必要剧情节拍、角色使用未知信息、
   与权威事实直接冲突、关键人物/时间/地点错误，或因果关系无法成立。
2. warning：伏笔推进不足、非关键空间动作不清、字数明显偏离目标、
   结尾钩子不足，或局部节奏和对话问题。
3. info：不影响剧情、连续性和计划遵循的可选润色建议。
4. 不要为了提高分数而弱化严重程度；同一问题只报告一次。

如需核验正文中已出现的具体人物状态、事实、伏笔、历史事件或稳定设定，可调用只读工具。
工具返回的是权威正史证据；“没有检索到结果”不等于正文错误，只有存在相反的
当前权威证据时才可报告连续性冲突。不得把工具当作扩写剧情或寻找挑错理由的手段。
若 chapter_plan.hook_plan.resolve_hook_ids 非空，必须检查正文是否给出了每条伏笔的明确答案、
真相或结果；只提及线索不算回收，应报告 plan_following 或 hook_consistency 问题。
总工具调用预算为 10 次，模型最多运行 4 回合。工具预算耗尽后，必须根据已经获得的
证据直接输出 ReviewReport，不得继续请求工具。

只返回 JSON 对象，包含 passed、summary、issues、score、parse_failed。
issues 每项包含 category、severity、description、suggestion、related_source_ids。
正常审查时 parse_failed 必须为 false；存在 critical 问题时 passed 必须为 false。
score 只能是 0 到 100 的 JSON 整数或 null，禁止使用字符串、小数、百分号或“分”。

合法 JSON 示例（issues 为空时）：
{"passed":true,"summary":"审查通过","issues":[],"score":90,"parse_failed":false}
"""

REVISION_VERIFICATION_SYSTEM_PROMPT = """你是 StoryWeaver 的章节定向复查员。
你只验证上一轮列出的待修复问题是否已在修订稿中解决，并检查修订是否造成明显的
正史冲突、人物知识越界或必要剧情节拍缺失。不要重新进行全面风格打分，不要寻找
新的普通润色问题，也不要要求第二次自动重写。

严重程度只能使用 info、warning、critical；类别只能使用既定审查类别。
若原问题已解决且没有明显硬回归，issues 必须为空且 passed 为 true。若仍有未解决的
硬问题，报告该问题；只有存在直接证据时才报告连续性冲突。工具预算为 4 次，最多一轮
检索；工具用完后直接交付。

只返回 JSON 对象，包含 passed、summary、issues、score、parse_failed。
"""

# DeepSeek 仅提供 json_object 模式；此 Schema 用于提示模型和本地边界校验，
# 防止将 issue 字段错误地放到审查报告顶层。
REVIEW_REPORT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passed", "summary", "issues", "score", "parse_failed"],
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "category",
                    "severity",
                    "description",
                    "suggestion",
                    "related_source_ids",
                ],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "plan_following",
                            "character_consistency",
                            "knowledge_boundary",
                            "world_continuity",
                            "hook_consistency",
                            "structure",
                            "style",
                        ],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "critical"],
                    },
                    "description": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "related_source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "score": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        "parse_failed": {"type": "boolean"},
    },
}


class ReviewerAgent(BaseNovelAgent[dict[str, Any]]):
    """生成结构化审查；不可解析时返回明确的失败报告。"""

    def __init__(
        self,
        *,
        renderer: ChapterContextRenderer | None = None,
        retry_policy: WorkerRetryPolicy | None = None,
        store: NovelProjectStore | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
        context_snapshot_sink: object | None = None,
    ) -> None:
        super().__init__(
            agent_id="novel-reviewer",
            name="章节审查员",
            system_prompt=REVIEWER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
        )
        self._renderer = renderer or ChapterContextRenderer()
        self._store = store
        self._context_snapshot_sink = context_snapshot_sink

    async def review(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
    ) -> ReviewReport:
        if self._store is None or context.book_id is None:
            raise ValueError("Reviewer 必须注入作品存储和 book_id")
        snapshot = self._load_snapshot(context)
        prompt = self._review_prompt(context=context, draft=draft, snapshot=snapshot)
        return await self._run_sdk_review(prompt, snapshot)

    async def verify_revision(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        original_review: ReviewReport,
    ) -> ReviewReport:
        """修订后只核验原硬问题，避免再次启动完整自主审查。"""

        if self._store is None or context.book_id is None:
            raise ValueError("Reviewer 必须注入作品存储和 book_id")
        snapshot = self._load_snapshot(context)
        prompt = self._verification_prompt(
            context=context,
            draft=draft,
            original_review=original_review,
        )
        settings = replace(
            self._sdk_settings,
            worker_id="novel-reviewer-verification",
            name="章节定向复查员",
            instructions=REVISION_VERIFICATION_SYSTEM_PROMPT,
        )
        return await self._run_sdk_review(
            prompt,
            snapshot,
            settings=settings,
            max_tool_calls=4,
            max_research_turns=1,
        )

        def convert(raw_report: dict[str, Any]) -> ReviewReport:
            return decode_review_report(self._normalize_score(raw_report))

        try:
            return await self._generate_validated_with_agent(
                prompt,
                convert,
                agent=agent,
                runtime=runtime,
                repair_instruction=(
                    "上一次审查报告未通过严格 JSON 校验。请保持审查结论基于"
                    "同一正文，只返回完整 ReviewReport JSON。顶层只能有 "
                    "passed、summary、issues、score、parse_failed；category、"
                    "severity、description、suggestion、related_source_ids 必须"
                    "只存在于 issues 数组的每一项中，绝不能出现在顶层。"
                ),
            )
        except SerializationError as exc:
            return ReviewReport(
                passed=False,
                summary=f"Reviewer 输出解析失败：{exc}",
                issues=(),
                score=None,
                parse_failed=True,
            )

    async def _run_sdk_review(
        self,
        prompt: str,
        snapshot: ReviewSnapshot,
        *,
        settings: WorkerSettings | None = None,
        max_tool_calls: int = 10,
        max_research_turns: int = 3,
    ) -> ReviewReport:
        active_settings = settings or self._sdk_settings
        evidence: list[dict[str, object]] = []
        async def completed(name: str, index: int, succeeded: bool, exhausted: bool, elapsed: float, error: str | None, deduplicated: bool) -> None:
            event = LlmEvent(LlmEventType.TOOL_COMPLETED, active_settings.worker_id, {
                "tool_name": name, "tool_call_index": index, "tool_call_limit": max_tool_calls,
                "succeeded": succeeded, "budget_exhausted": exhausted, "elapsed_seconds": elapsed,
                "error": error, "deduplicated": deduplicated,
            })
            for sink in self._event_sinks:
                await sink.on_event(event)
        tools = build_sdk_read_tools(snapshot=snapshot, max_tool_calls=max_tool_calls, on_completed=completed, evidence=evidence)
        async def operation(_context: Any) -> ReviewReport:
            output = await run_research_then_submit(
                settings=active_settings, prompt=prompt, read_tools=tools,
                output_type=NOVEL_OUTPUT_TYPES["novel-reviewer"], submit_tool_name="submit_review",
                submit_description="提交最终审查报告；不会修改章节、状态或质量门禁。",
                max_research_turns=max_research_turns, evidence=evidence, event_sinks=self._event_sinks,
                tracing_enabled=True,
            )
            return decode_review_report(self._normalize_score(output.model_dump()))
        try:
            return await run_with_retry(
                worker_name=active_settings.worker_id,
                operation=operation,
                policy=self._sdk_retry_policy,
            )
        finally:
            self._record_context_snapshot(
                agent_role=active_settings.worker_id.removeprefix("novel-"),
                prompt=prompt,
                snapshot=snapshot,
                evidence=evidence,
            )

    def _record_context_snapshot(
        self,
        *,
        agent_role: str,
        prompt: str,
        snapshot: ReviewSnapshot,
        evidence: list[dict[str, object]],
    ) -> None:
        """保存审查初始上下文与只读检索证据，不干扰质量门禁。"""

        sink = self._context_snapshot_sink
        if sink is None:
            return
        try:
            book_version = self._book_version(snapshot.project)
            candidate = ContextCandidate(
                source=source_ref(
                    source_id=f"{agent_role}:initial-prompt",
                    source_type="reviewer_initial_context",
                    content=prompt,
                    book_version=book_version,
                ),
                content=prompt,
                reason="审查员初始上下文：计划、正文及最小正史索引",
                protected=True,
                priority=100,
            )
            _, trace = trace_from_candidates(
                agent_role=agent_role,
                policy_version="reviewer-context-v2.1",
                book_version=book_version,
                token_budget=max(1, len(prompt) // 2 + 16),
                candidates=(candidate,),
                notes=("工具证据由 research 阶段追加",),
            )
            trace = with_tool_results(trace, evidence=evidence)
            sink.save(
                agent_role=agent_role,
                book_id=snapshot.project.metadata.book_id,
                book_version=book_version,
                policy_version=trace.policy_version,
                renderer_version=trace.renderer_version,
                rendered_context=prompt,
                trace=trace.to_data(),
                job_id=get_log_context().get("run_id"),
            )
        except Exception:
            return

    def _book_version(self, project: object) -> int:
        loader = getattr(self._store, "load_project_with_version", None)
        metadata = getattr(project, "metadata")
        state = getattr(project, "state")
        if callable(loader):
            return int(loader(metadata.book_id)[1])
        return int(state.last_committed_chapter)

    def _load_snapshot(self, context: ChapterContext) -> ReviewSnapshot:
        """在审查开始前读取一次权威状态，后续工具只使用这份快照。"""

        assert self._store is not None
        assert context.book_id is not None
        project = self._store.load_project(context.book_id)
        expected_base = context.chapter_number - 1
        if project.state.last_committed_chapter != expected_base:
            raise ValueError(
                "Reviewer 上下文基线已过期："
                f"期望第 {expected_base} 章，实际第 {project.state.last_committed_chapter} 章"
            )
        return ReviewSnapshot(
            project=project,
            chapter_summaries=self._store.load_chapter_summaries(context.book_id),
        )

    def _tool_runtime(
        self,
        snapshot: ReviewSnapshot,
    ) -> tuple[AgentRuntime, Agent[dict[str, Any]]]:
        """为本次审查绑定隔离工具注册表，避免跨作品或跨请求读取错误快照。"""

        registry = ToolRegistry()
        registry.register(EntityEvidenceTool(snapshot))
        registry.register(CanonEvidenceSearchTool(snapshot))
        registry.register(ChapterSummaryTool(snapshot))
        registry.register(FoundationQueryTool(snapshot))
        registry.register(OpenForeshadowingsTool(snapshot))
        executor = ToolExecutor(
            registry,
            permission_policy=self._runtime.tool_executor.permission_policy,
        )
        if isinstance(self._runtime, OpenAIAgentsRuntime):
            runtime = self._runtime.with_tools(
                tool_registry=registry,
                tool_executor=executor,
                max_tool_calls=10,
            )
        else:
            runtime = AgentRuntime(
            tool_registry=registry,
            tool_executor=executor,
            context_builder=self._runtime.context_builder,
            hooks=self._runtime.hooks,
            # canon 证据必须保持原文，不能被归档摘要改写。
            tool_result_archive=None,
            max_inline_tool_result_tokens=self._runtime.max_inline_tool_result_tokens,
            max_tool_calls=10,
            )
        agent = Agent(
            agent_id=self._agent.agent_id,
            name=self._agent.name,
            system_prompt=self._agent.system_prompt,
            model=self._agent.model,
            output_schema=dict,
            output_json_schema=REVIEW_REPORT_JSON_SCHEMA,
            allowed_tools=(
                "get_entity_evidence",
                "search_canon_evidence",
                "read_chapter_summary",
                "query_foundation",
                "list_open_foreshadowings",
            ),
            config=AgentConfig(
                max_steps=4,
                model_timeout_seconds=self._agent.config.model_timeout_seconds,
                temperature=self._agent.config.temperature,
                max_structured_repairs=self._agent.config.max_structured_repairs,
            ),
        )
        return runtime, agent

    def _review_prompt(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        snapshot: ReviewSnapshot,
    ) -> str:
        """构造审查专用最小上下文；详细 canon 由工具按需取得。"""

        plan = self._plan_from_context(context)
        state_by_id = {
            item.character_id: item
            for item in snapshot.project.state.characters
        }
        participants = [
            {
                "character_id": item.character_id,
                "location": item.location,
                "status": item.status,
                "current_goal": item.current_goal,
                "possessions": item.possessions,
            }
            for character_id in plan.participating_character_ids
            if (item := state_by_id.get(character_id)) is not None
        ]
        hook_by_id = {item.hook_id: item for item in snapshot.project.state.hooks}
        relevant_hooks = [
            {
                "hook_id": item.hook_id,
                "name": item.display_name,
                "status": item.status,
                "source_chapter": item.opened_chapter,
                "importance": item.importance,
            }
            for hook_id in plan.relevant_hook_ids
            if (item := hook_by_id.get(hook_id)) is not None
        ]
        fact_index = [
            {
                "fact_id": item.fact_id,
                "subject_id": item.subject_id,
                "predicate": item.predicate,
                "source_chapter": item.source_chapter,
                "importance": item.importance,
            }
            for item in sorted(
                snapshot.project.state.current_facts,
                key=lambda item: (-item.importance, -item.source_chapter, item.fact_id),
            )[:5]
        ]
        latest_summary = snapshot.chapter_summaries[-1:] if snapshot.chapter_summaries else ()
        payload = {
            "chapter_plan": to_data(plan),
            "book_constraints": {
                "title": snapshot.project.metadata.title,
                "genre": snapshot.project.metadata.genre,
                "chapter_target_words": snapshot.project.metadata.chapter_target_words,
            },
            "stable_foundation": {
                "premise": snapshot.project.foundation.premise,
                "world_setting": snapshot.project.foundation.world_setting,
                "central_conflict": snapshot.project.foundation.central_conflict,
                "writing_rules": snapshot.project.foundation.writing_rules,
            },
            "current_story_position": {
                "last_committed_chapter": snapshot.project.state.last_committed_chapter,
                "current_time": snapshot.project.state.current_time,
                "current_location": snapshot.project.state.current_location,
            },
            "participating_character_states": participants,
            "relevant_hook_index": relevant_hooks,
            "high_importance_fact_index": fact_index,
            "latest_chapter_summary": to_data(latest_summary[0]) if latest_summary else None,
            "user_instruction": self._user_instruction_from_context(context),
        }
        return (
            "请审查以下章节。下方只提供最小审查上下文和索引；"
            "遇到具体连续性疑点时，可使用只读工具获取完整权威证据。\n\n"
            "## 最小审查上下文\n"
            + dumps_json(payload)
            + "\n\n## 待审查正文\n"
            + dumps_json(to_data(draft))
        )

    def _verification_prompt(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
        original_review: ReviewReport,
    ) -> str:
        """复查只传递原问题和修订稿，缩小模型的判断空间。"""

        return (
            "请验证修订稿是否解决了以下原审查问题。只在确有必要时查询权威证据；"
            "不要重新做全面审查。\n\n## 原待修复问题\n"
            + dumps_json(to_data(original_review.issues))
            + "\n\n## 章节计划\n"
            + dumps_json(to_data(self._plan_from_context(context)))
            + "\n\n## 修订后正文\n"
            + dumps_json(to_data(draft))
        )

    def _legacy_prompt(self, *, context: ChapterContext, draft: ChapterDraft) -> str:
        """未注入 Store 的独立 Reviewer 继续兼容原有单步调用。"""

        return (
            "请审查以下章节。\n\n"
            + self._renderer.render_entries(context)
            + "\n\n## 待审查正文\n"
            + dumps_json(to_data(draft))
        )

    @staticmethod
    def _plan_from_context(context: ChapterContext) -> ChapterPlan:
        entry = next(
            (item for item in context.entries if item.source_type == "chapter_plan"),
            None,
        )
        if entry is None:
            raise ValueError("Reviewer 上下文缺少章节计划")
        return decode_chapter_plan(loads_json(entry.content))

    @staticmethod
    def _user_instruction_from_context(context: ChapterContext) -> str | None:
        entry = next(
            (item for item in context.entries if item.source_type == "user_instruction"),
            None,
        )
        return entry.content if entry is not None else None

    @classmethod
    def _normalize_score(cls, raw_report: dict[str, Any]) -> dict[str, Any]:
        """兼容模型常见数值表示，其余审查字段仍严格解析。"""

        if "score" not in raw_report:
            return raw_report
        score = raw_report["score"]
        if score is None or isinstance(score, int) and not isinstance(score, bool):
            return raw_report
        if isinstance(score, bool):
            return raw_report

        numeric_text: str | None = None
        if isinstance(score, float):
            numeric_text = str(score)
        elif isinstance(score, str):
            stripped = score.strip()
            if stripped.casefold() in {"null", "none"}:
                return {**raw_report, "score": None}
            matched = re.fullmatch(
                r"(\d+(?:\.\d+)?)\s*(?:/\s*100|分)?",
                stripped,
            )
            if matched:
                numeric_text = matched.group(1)

        if numeric_text is None:
            return raw_report
        try:
            numeric_score = Decimal(numeric_text)
        except InvalidOperation:
            return raw_report
        if not numeric_score.is_finite() or not 0 <= numeric_score <= 100:
            return raw_report
        normalized = int(numeric_score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return {**raw_report, "score": normalized}
