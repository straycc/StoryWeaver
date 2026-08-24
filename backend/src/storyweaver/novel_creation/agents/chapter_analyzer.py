"""从最终章节提取候选状态增量的 ChapterAnalyzer Agent。"""

from __future__ import annotations

import logging
from typing import Any

from ...llm import LlmEventSink, WorkerRetryPolicy, WorkerSettings
from ..exceptions import ChapterAnalysisError, SerializationError, StateTransitionError
from ..hook_manager import HookManager
from ...observability import logging_context
from ..models import ChapterDraft, ChapterPlan, NovelProject, StoryStateDelta
from ..serialization import decode_story_state_delta, dumps_json, to_data
from ..state_reducer import NovelStateReducer
from .base import BaseNovelAgent


CHAPTER_ANALYZER_SYSTEM_PROMPT = """你是 StoryWeaver 的章节状态分析器。
你不评价文风，也不续写正文；只从最终正文中提取本章已经发生的状态变化。

约束：
1. source_chapter 必须等于输入章节号。
2. 只记录正文明确发生的变化，不把计划中尚未发生的内容写入状态。
3. 新事实、角色学习和伏笔推进必须使用稳定 ID。
4. 不修改不存在的角色、事实或伏笔；新事实不得预先失效。
   new_facts.fact_id 不得与输入 existing_fact_index 或 reserved_fact_ids 中的任何 ID 重复；
   已有事实仍然成立时不输出它，已有事实失效时仅写入 invalidated_fact_ids。
5. learned_fact_ids 只能引用 current_state.current_facts 中已有的 fact_id，
   或本次 new_facts 中同时声明的 fact_id；不能只让角色学习一个未声明的新 ID。
6. 只返回 JSON 对象，不返回解释、Markdown 或 JSON Schema。
7. new_hooks 每章最多只能有一项；如正文是在推进已有线索，必须写入 hook_updates，
   不得为同一人物、物品或谜团另建名称相近的新伏笔。
8. 输入 chapter_plan.hook_plan.resolve_hook_ids 是本章计划回收的伏笔。仅当正文已经给出
   明确答案、真相或结果时，将对应 hook_updates.status 写为 resolved；未实际兑现时不得伪造 resolved。

JSON 必须包含：source_chapter、chapter_summary、character_updates、new_facts、
invalidated_fact_ids、new_hooks、hook_updates、new_time、new_location。
character_updates、new_facts、invalidated_fact_ids、new_hooks、hook_updates 均为数组。

character_updates 每项只能包含：character_id、location、status、current_goal、
emotion、add_possessions、remove_possessions、learned_fact_ids。
其中 add_possessions、remove_possessions、learned_fact_ids 都是“本章变化量”，
禁止输出完整状态字段 possessions 或 known_fact_ids；没有变化时使用空数组。

new_facts 每项包含：fact_id、subject_id、predicate、value、valid_from_chapter、
valid_until_chapter、source_chapter、importance。
hook_updates 每项包含：hook_id、status、note；status 只能是
open、progressing、resolved 或 deferred，“本章推进但未解决”必须使用 progressing。
new_hooks 每项包含：hook_id、name、description、status、importance、opened_chapter、
last_advanced_chapter、expected_payoff；name 是供作者阅读的简短中文名称，
hook_id 仍使用英文小写连字符格式；status 同样只能使用上述四个值。
"""

_LOGGER = logging.getLogger(__name__)


class ChapterAnalyzerAgent(BaseNovelAgent[dict[str, Any]]):
    """把最终正文转换为候选 ``StoryStateDelta``。"""

    _HOOK_STATUS_ALIASES = {
        "advanced": "progressing",
        "active": "progressing",
        "in_progress": "progressing",
        "completed": "resolved",
        "closed": "resolved",
        "pending": "open",
        "paused": "deferred",
    }

    def __init__(
        self,
        *,
        retry_policy: WorkerRetryPolicy | None = None,
        hook_manager: HookManager | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
    ) -> None:
        super().__init__(
            agent_id="chapter-analyzer",
            name="章节状态分析器",
            system_prompt=CHAPTER_ANALYZER_SYSTEM_PROMPT,
            output_schema=dict,
            retry_policy=retry_policy,
            sdk_settings=sdk_settings,
            event_sinks=event_sinks,
        )
        self._hook_manager = hook_manager or HookManager()

    async def analyze(
        self,
        *,
        project: NovelProject,
        plan: ChapterPlan,
        draft: ChapterDraft,
    ) -> StoryStateDelta:
        analysis_input = {
            "chapter_number": draft.chapter_number,
            "plan": plan,
            "final_draft": draft,
            "current_state": {
                "last_committed_chapter": project.state.last_committed_chapter,
                "current_time": project.state.current_time,
                "current_location": project.state.current_location,
                "characters": project.state.characters,
                "current_facts": project.state.current_facts,
                "reserved_fact_ids": tuple(fact.fact_id for fact in project.state.facts),
                "existing_fact_index": tuple(
                    {
                        "fact_id": fact.fact_id,
                        "subject_id": fact.subject_id,
                        "predicate": fact.predicate,
                        "value": fact.value,
                        "source_chapter": fact.source_chapter,
                        "status": "current" if fact.is_current else "superseded",
                    }
                    for fact in project.state.facts
                ),
                "hooks": project.state.hooks,
            },
        }
        serialized_input = dumps_json(to_data(analysis_input))
        analysis_prompt = "请分析以下最终章节并提取状态增量。\n\n" + serialized_input

        def convert(raw_delta: dict[str, Any]) -> StoryStateDelta:
            self._reject_reused_fact_ids(raw_delta, project=project)
            normalized = self._normalize_character_state_snapshots(
                raw_delta,
                project=project,
            )
            normalized = self._normalize_hook_statuses(normalized)
            try:
                delta = decode_story_state_delta(normalized)
            except SerializationError as exc:
                raise ChapterAnalysisError(
                    f"ChapterAnalyzer 输出无法转换为状态增量：{exc}"
                ) from exc
            if delta.source_chapter != draft.chapter_number:
                raise ChapterAnalysisError(
                    f"状态增量章节号不一致：期望 {draft.chapter_number}，"
                    f"实际 {delta.source_chapter}"
                )
            delta, governance = self._hook_manager.reconcile_with_report(
                project=project,
                delta=delta,
                plan=plan,
            )
            self._log_hook_governance(governance)
            try:
                # 预演 StateReducer，提前发现 learned_fact_ids 等跨字段引用错误。
                # 此处不会写入存储，真正提交仍由 Pipeline 在事务内完成。
                NovelStateReducer().apply(project.state, delta)
            except StateTransitionError as exc:
                raise ChapterAnalysisError(
                    f"状态增量无法应用到当前正史：{exc}"
                ) from exc
            return delta

        return await self._generate_validated(
            analysis_prompt,
            convert,
            repair_instruction=(
                "上一次响应不是可解析的 JSON 对象，或未通过状态增量校验。"
                "尤其要确保 learned_fact_ids 引用当前已有事实或本次 new_facts。"
                "new_facts.fact_id 必须是输入 reserved_fact_ids 中从未出现过的新 ID；"
                "已有事实不应重复创建。"
                "new_hooks 最多一项，优先更新已有伏笔。"
                "请根据校验错误重新分析，只输出一个完整 JSON 对象，"
                "不要输出说明、Markdown 或多个候选对象。"
            ),
        )

    @staticmethod
    def _reject_reused_fact_ids(
        raw_delta: dict[str, Any],
        *,
        project: NovelProject,
    ) -> None:
        """在 Reducer 预演前给模型可修复的精确事实 ID 冲突信息。"""

        raw_facts = raw_delta.get("new_facts")
        if not isinstance(raw_facts, (list, tuple)):
            return
        existing_ids = {fact.fact_id for fact in project.state.facts}
        reused_ids = sorted(
            item.get("fact_id")
            for item in raw_facts
            if isinstance(item, dict)
            and isinstance(item.get("fact_id"), str)
            and item["fact_id"] in existing_ids
        )
        if reused_ids:
            raise ChapterAnalysisError(
                "new_facts 不得复用已有事实 ID：" + ", ".join(reused_ids)
            )

    @staticmethod
    def _log_hook_governance(governance: object) -> None:
        """输出能关联到当前请求的伏笔治理摘要。"""

        # 避免将 HookManager 与日志框架绑定；Analyzer 是该领域动作的调用边界。
        advanced = getattr(governance, "advanced_hook_ids", ())
        created = getattr(governance, "created_hook_ids", ())
        merged = getattr(governance, "merged_pairs", ())
        dropped = getattr(governance, "dropped_hook_ids", ())
        resolved = getattr(governance, "resolved_hook_ids", ())
        missed = getattr(governance, "missed_resolution_ids", ())
        ending_risks = getattr(governance, "ending_risk_hook_ids", ())
        merged_text = (
            ", ".join(f"{source}→{target}" for source, target in merged)
            if merged
            else "无"
        )
        with logging_context(agent_id="chapter-analyzer"):
            _LOGGER.info(
                "伏笔治理 | 推进 %d%s · 新增 %d%s · 合并 %d（%s）· 超预算忽略 %d%s",
                len(advanced),
                f"（{', '.join(advanced)}）" if advanced else "",
                len(created),
                f"（{', '.join(created)}）" if created else "",
                len(merged),
                merged_text,
                len(dropped),
                f"（{', '.join(dropped)}）" if dropped else "",
            )
            if resolved or missed or ending_risks:
                _LOGGER.info(
                    "伏笔回收 | 已回收 %d%s · 计划未兑现 %d%s · 结局遗留 %d%s",
                    len(resolved),
                    f"（{', '.join(resolved)}）" if resolved else "",
                    len(missed),
                    f"（{', '.join(missed)}）" if missed else "",
                    len(ending_risks),
                    f"（{', '.join(ending_risks)}）" if ending_risks else "",
                )

    @classmethod
    def _normalize_hook_statuses(
        cls,
        raw_delta: dict[str, Any],
    ) -> dict[str, Any]:
        """将模型常用的伏笔状态同义词收敛为领域枚举。"""

        normalized = dict(raw_delta)
        for field_name in ("hook_updates", "new_hooks"):
            raw_items = raw_delta.get(field_name)
            if not isinstance(raw_items, (list, tuple)):
                continue
            items: list[object] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    items.append(raw_item)
                    continue
                item = dict(raw_item)
                status = item.get("status")
                if isinstance(status, str):
                    canonical = status.strip().casefold().replace("-", "_")
                    item["status"] = cls._HOOK_STATUS_ALIASES.get(
                        canonical,
                        canonical,
                    )
                items.append(item)
            normalized[field_name] = items
        return normalized

    @classmethod
    def _normalize_character_state_snapshots(
        cls,
        raw_delta: dict[str, Any],
        *,
        project: NovelProject,
    ) -> dict[str, Any]:
        """把模型偶尔返回的角色完整状态转换为真正的变化量。"""

        updates = raw_delta.get("character_updates")
        if not isinstance(updates, (list, tuple)):
            return raw_delta

        current_characters = {
            character.character_id: character
            for character in project.state.characters
        }
        normalized_updates: list[object] = []
        for raw_update in updates:
            if not isinstance(raw_update, dict):
                normalized_updates.append(raw_update)
                continue
            update = dict(raw_update)
            character_id = update.get("character_id")
            current = current_characters.get(character_id)

            if "possessions" in update:
                if current is None:
                    raise ChapterAnalysisError(
                        f"Analyzer 为未知角色 {character_id!r} 返回了 possessions"
                    )
                target_possessions = cls._string_sequence(
                    update.pop("possessions"),
                    "possessions",
                )
                additions = tuple(
                    item
                    for item in target_possessions
                    if item not in current.possessions
                )
                removals = tuple(
                    item
                    for item in current.possessions
                    if item not in target_possessions
                )
                update["add_possessions"] = cls._merge_string_sequences(
                    update.get("add_possessions", ()),
                    additions,
                    field_name="add_possessions",
                )
                update["remove_possessions"] = cls._merge_string_sequences(
                    update.get("remove_possessions", ()),
                    removals,
                    field_name="remove_possessions",
                )

            if "known_fact_ids" in update:
                if current is None:
                    raise ChapterAnalysisError(
                        f"Analyzer 为未知角色 {character_id!r} 返回了 known_fact_ids"
                    )
                target_fact_ids = cls._string_sequence(
                    update.pop("known_fact_ids"),
                    "known_fact_ids",
                )
                learned = tuple(
                    fact_id
                    for fact_id in target_fact_ids
                    if fact_id not in current.known_fact_ids
                )
                update["learned_fact_ids"] = cls._merge_string_sequences(
                    update.get("learned_fact_ids", ()),
                    learned,
                    field_name="learned_fact_ids",
                )

            if "remove_possessions" in update and current is not None:
                requested_removals = cls._string_sequence(
                    update["remove_possessions"],
                    "remove_possessions",
                )
                # 物品持有是集合状态。正文可能提到角色失去一个此前未被状态层
                # 登记的临时物品；对该集合执行删除应是幂等操作，不应阻断整章。
                update["remove_possessions"] = [
                    item for item in requested_removals if item in current.possessions
                ]

            normalized_updates.append(update)

        return {**raw_delta, "character_updates": normalized_updates}

    @staticmethod
    def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ChapterAnalysisError(
                f"Analyzer 字段 {field_name} 必须是非空字符串数组"
            )
        return tuple(value)

    @classmethod
    def _merge_string_sequences(
        cls,
        first: object,
        second: tuple[str, ...],
        *,
        field_name: str,
    ) -> list[str]:
        merged = list(cls._string_sequence(first, field_name))
        for item in second:
            if item not in merged:
                merged.append(item)
        return merged
