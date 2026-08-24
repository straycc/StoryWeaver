"""为单章写作选择有限、可追踪的小说上下文。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from ..memory.long_term import LongTermMemoryRecord, LongTermMemoryType

from .models import (
    ChapterContext,
    ChapterPlan,
    ChapterSummary,
    ContextEntry,
    ContextTrace,
    FactRecord,
    NovelProject,
    StoryHook,
)
from .serialization import dumps_json, to_data
from .validation import ChapterPlanValidator


class ChapterContextBuilder:
    """按确定性规则选择 Writer 所需资料，并记录选择轨迹。"""

    def __init__(
        self,
        *,
        token_budget: int = 6000,
        recent_summary_limit: int = 3,
        fact_limit: int = 10,
        ordinary_hook_limit: int = 5,
        token_estimator: Callable[[str], int] | None = None,
        plan_validator: ChapterPlanValidator | None = None,
        creative_control_provider: Callable[[str], object] | None = None,
    ) -> None:
        if token_budget <= 0:
            raise ValueError("token_budget 必须大于 0")
        for name, value in (
            ("recent_summary_limit", recent_summary_limit),
            ("fact_limit", fact_limit),
            ("ordinary_hook_limit", ordinary_hook_limit),
        ):
            if value < 0:
                raise ValueError(f"{name} 不能小于 0")
        self.token_budget = token_budget
        self.recent_summary_limit = recent_summary_limit
        self.fact_limit = fact_limit
        self.ordinary_hook_limit = ordinary_hook_limit
        self._estimate_content_tokens = token_estimator or self._default_token_estimator
        self._plan_validator = plan_validator or ChapterPlanValidator()
        self._creative_control_provider = creative_control_provider

    def build(
        self,
        *,
        project: NovelProject,
        plan: ChapterPlan,
        chapter_summaries: tuple[ChapterSummary, ...] = (),
        user_instruction: str | None = None,
        long_term_memories: tuple[LongTermMemoryRecord, ...] = (),
    ) -> tuple[ChapterContext, ContextTrace]:
        """构造有限上下文和选择 Trace。"""

        self._plan_validator.validate(project=project, plan=plan)
        if not isinstance(chapter_summaries, tuple):
            raise TypeError("chapter_summaries 必须是 tuple")
        if user_instruction is not None and not user_instruction.strip():
            raise ValueError("user_instruction 必须为非空字符串或 None")

        protected_entries = self._build_protected_entries(
            project=project,
            plan=plan,
            user_instruction=user_instruction,
            long_term_memories=long_term_memories,
        )
        candidate_entries, pre_excluded = self._build_candidate_entries(
            project=project,
            plan=plan,
            chapter_summaries=chapter_summaries,
        )

        selected_entries = list(protected_entries)
        used_tokens = sum(self._entry_tokens(entry) for entry in protected_entries)
        excluded_ids = list(pre_excluded)
        notes: list[str] = []
        if used_tokens > self.token_budget:
            notes.append(
                "受保护上下文估算 Token 已超过预算；为保证计划和连续性约束，"
                "本次未删除受保护条目。"
            )

        for entry in sorted(
            candidate_entries,
            key=lambda item: (-item.priority, item.source_id),
        ):
            entry_tokens = self._entry_tokens(entry)
            if used_tokens + entry_tokens <= self.token_budget:
                selected_entries.append(entry)
                used_tokens += entry_tokens
            else:
                excluded_ids.append(entry.source_id)

        selected_ids = tuple(entry.source_id for entry in selected_entries)
        selected_id_set = set(selected_ids)
        excluded_ids = [
            source_id
            for source_id in self._unique(excluded_ids)
            if source_id not in selected_id_set
        ]
        protected_ids = tuple(
            entry.source_id for entry in protected_entries if entry.protected
        )
        notes.append(
            f"选择 {len(selected_entries)} 个来源，排除 {len(excluded_ids)} 个来源；"
            f"估算使用 {used_tokens}/{self.token_budget} Token。"
        )

        context = ChapterContext(
            chapter_number=plan.chapter_number,
            entries=tuple(selected_entries),
            estimated_tokens=used_tokens,
            book_id=project.metadata.book_id,
        )
        trace = ContextTrace(
            chapter_number=plan.chapter_number,
            selected_source_ids=selected_ids,
            excluded_source_ids=tuple(excluded_ids),
            protected_source_ids=protected_ids,
            budget=self.token_budget,
            notes=tuple(notes),
        )
        return context, trace

    def _build_protected_entries(
        self,
        *,
        project: NovelProject,
        plan: ChapterPlan,
        user_instruction: str | None,
        long_term_memories: tuple[LongTermMemoryRecord, ...],
    ) -> tuple[ContextEntry, ...]:
        entries = [
            self._entry(
                source_id=f"plan:{plan.chapter_number}",
                source_type="chapter_plan",
                value=plan,
                reason="本章写作必须遵守的计划",
                protected=True,
                priority=100,
            ),
            self._entry(
                source_id="book-constraints",
                source_type="book_constraints",
                value={
                    "title": project.metadata.title,
                    "genre": project.metadata.genre,
                    "language": project.metadata.language,
                    "target_chapters": project.metadata.target_chapters,
                    "chapter_target_words": project.metadata.chapter_target_words,
                },
                reason="本章必须遵守的书籍级输出约束",
                protected=True,
                priority=100,
            ),
            self._entry(
                source_id="story-foundation",
                source_type="story_foundation",
                value={
                    "premise": project.foundation.premise,
                    "world_setting": project.foundation.world_setting,
                    "central_conflict": project.foundation.central_conflict,
                    "ending_direction": project.foundation.ending_direction,
                },
                reason="小说不可偏离的世界和主线基础",
                protected=True,
                priority=100,
            ),
            self._entry(
                source_id="writing-rules",
                source_type="writing_rules",
                value=project.foundation.writing_rules,
                reason="小说全局硬规则",
                protected=True,
                priority=100,
            ),
            self._entry(
                source_id=f"state:{project.state.last_committed_chapter}",
                source_type="current_state",
                value={
                    "last_committed_chapter": project.state.last_committed_chapter,
                    "current_time": project.state.current_time,
                    "current_location": project.state.current_location,
                    "characters": project.state.characters,
                },
                reason="上一章结束后的权威动态状态",
                protected=True,
                priority=100,
            ),
        ]
        if self._creative_control_provider is not None:
            control = self._creative_control_provider(project.metadata.book_id)
            author_intent = str(getattr(control, "author_intent", "")).strip()
            current_focus = str(getattr(control, "current_focus", "")).strip()
            focus_mode = str(getattr(control, "current_focus_mode", "persistent"))
            focus_target = getattr(control, "focus_target_chapter", None)
            if focus_mode == "single_chapter" and focus_target != plan.chapter_number:
                current_focus = ""
            if author_intent or current_focus:
                entries.append(
                    self._entry(
                        source_id="creative-control",
                        source_type="creative_control",
                        value={"author_intent": author_intent, "current_focus": current_focus},
                        reason="作者当前明确的长期意图与近期焦点",
                        protected=True,
                        priority=100,
                    )
                )
        if user_instruction is not None:
            entries.append(
                ContextEntry(
                    source_id=f"user-instruction:{plan.chapter_number}",
                    source_type="user_instruction",
                    content=user_instruction.strip(),
                    reason="用户对本章的直接要求",
                    protected=True,
                    priority=100,
                )
            )

        for memory in long_term_memories:
            if memory.memory_type == LongTermMemoryType.REFERENCE:
                continue
            entries.append(
                self._entry(
                    source_id=f"long-term-memory:{memory.memory_id}",
                    source_type=memory.memory_type.value,
                    value={
                        "description": memory.description,
                        "content": memory.content,
                        "scope": f"{memory.scope_type.value}:{memory.scope_id}",
                    },
                    reason="用户跨会话偏好或当前作品长期指令",
                    protected=True,
                    priority=96,
                )
            )

        profiles = {
            character.character_id: character
            for character in project.foundation.characters
        }
        for character_id in plan.participating_character_ids:
            entries.append(
                self._entry(
                    source_id=f"character:{character_id}",
                    source_type="character_profile",
                    value=profiles[character_id],
                    reason="本章参与角色的稳定设定和知识边界",
                    protected=True,
                    priority=95,
                )
            )

        hooks = {hook.hook_id: hook for hook in project.state.hooks}
        for hook_id in plan.relevant_hook_ids:
            entries.append(
                self._entry(
                    source_id=f"hook:{hook_id}",
                    source_type="story_hook",
                    value=hooks[hook_id],
                    reason="章节计划明确要求处理的伏笔",
                    protected=True,
                    priority=95,
                )
            )
        return tuple(entries)

    def _build_candidate_entries(
        self,
        *,
        project: NovelProject,
        plan: ChapterPlan,
        chapter_summaries: tuple[ChapterSummary, ...],
    ) -> tuple[tuple[ContextEntry, ...], tuple[str, ...]]:
        entries: list[ContextEntry] = []
        excluded: list[str] = []

        participant_ids = set(plan.participating_character_ids)
        excluded.extend(
            f"character:{character.character_id}"
            for character in project.foundation.characters
            if character.character_id not in participant_ids
        )

        valid_summaries = sorted(
            (
                summary
                for summary in chapter_summaries
                if summary.chapter_number < plan.chapter_number
            ),
            key=lambda item: item.chapter_number,
            reverse=True,
        )
        selected_summaries = valid_summaries[: self.recent_summary_limit]
        excluded.extend(
            f"summary:{summary.chapter_number}"
            for summary in valid_summaries[self.recent_summary_limit :]
        )
        for rank, summary in enumerate(selected_summaries):
            entries.append(
                self._entry(
                    source_id=f"summary:{summary.chapter_number}",
                    source_type="chapter_summary",
                    value=summary,
                    reason="最近已提交章节的摘要",
                    protected=False,
                    priority=max(70, 80 - rank),
                )
            )

        hook_entries, hook_excluded = self._select_hook_entries(
            hooks=project.state.hooks,
            relevant_hook_ids=set(plan.relevant_hook_ids),
        )
        entries.extend(hook_entries)
        excluded.extend(hook_excluded)

        fact_entries, fact_excluded = self._select_fact_entries(
            facts=project.state.current_facts,
            plan=plan,
            last_committed_chapter=project.state.last_committed_chapter,
        )
        entries.extend(fact_entries)
        excluded.extend(fact_excluded)
        return tuple(entries), tuple(self._unique(excluded))

    def _select_hook_entries(
        self,
        *,
        hooks: tuple[StoryHook, ...],
        relevant_hook_ids: set[str],
    ) -> tuple[tuple[ContextEntry, ...], tuple[str, ...]]:
        unresolved = [
            hook
            for hook in hooks
            if hook.hook_id not in relevant_hook_ids and hook.status != "resolved"
        ]
        resolved_ids = [
            f"hook:{hook.hook_id}"
            for hook in hooks
            if hook.hook_id not in relevant_hook_ids and hook.status == "resolved"
        ]
        core_hooks = sorted(
            (hook for hook in unresolved if hook.importance >= 4),
            key=lambda hook: (-hook.importance, -hook.last_advanced_chapter, hook.hook_id),
        )
        ordinary_hooks = sorted(
            (hook for hook in unresolved if hook.importance < 4),
            key=lambda hook: (-hook.importance, -hook.last_advanced_chapter, hook.hook_id),
        )
        selected_ordinary = ordinary_hooks[: self.ordinary_hook_limit]
        excluded = resolved_ids + [
            f"hook:{hook.hook_id}"
            for hook in ordinary_hooks[self.ordinary_hook_limit :]
        ]
        entries = tuple(
            self._entry(
                source_id=f"hook:{hook.hook_id}",
                source_type="story_hook",
                value=hook,
                reason=(
                    "仍未解决的核心伏笔"
                    if hook.importance >= 4
                    else "仍未解决的普通伏笔"
                ),
                protected=False,
                priority=(85 + hook.importance if hook.importance >= 4 else 60 + hook.importance),
            )
            for hook in (*core_hooks, *selected_ordinary)
        )
        return entries, tuple(excluded)

    def _select_fact_entries(
        self,
        *,
        facts: tuple[FactRecord, ...],
        plan: ChapterPlan,
        last_committed_chapter: int,
    ) -> tuple[tuple[ContextEntry, ...], tuple[str, ...]]:
        query_terms = self._terms(
            " ".join(
                (
                    plan.goal,
                    plan.location,
                    plan.ending_hook,
                    *plan.required_beats,
                    *plan.style_focus,
                    *plan.participating_character_ids,
                )
            )
        )
        participant_ids = set(plan.participating_character_ids)
        scored: list[tuple[int, FactRecord]] = []
        for fact in facts:
            fact_terms = self._terms(
                f"{fact.subject_id} {fact.predicate} {fact.value}"
            )
            overlap = len(query_terms & fact_terms)
            recency = max(0, 10 - (last_committed_chapter - fact.source_chapter))
            participant_bonus = 20 if fact.subject_id in participant_ids else 0
            score = fact.importance * 10 + overlap * 6 + recency + participant_bonus
            scored.append((score, fact))
        scored.sort(key=lambda item: (-item[0], item[1].fact_id))

        selected = scored[: self.fact_limit]
        excluded = tuple(
            f"fact:{fact.fact_id}" for _, fact in scored[self.fact_limit :]
        )
        entries = tuple(
            self._entry(
                source_id=f"fact:{fact.fact_id}",
                source_type="story_fact",
                value=fact,
                reason="根据计划关键词、人物、重要性和新近度选择的当前事实",
                protected=False,
                priority=min(89, 40 + score // 2),
            )
            for score, fact in selected
        )
        return entries, excluded

    def _entry(
        self,
        *,
        source_id: str,
        source_type: str,
        value: object,
        reason: str,
        protected: bool,
        priority: int,
    ) -> ContextEntry:
        return ContextEntry(
            source_id=source_id,
            source_type=source_type,
            content=dumps_json(to_data(value), pretty=False),
            reason=reason,
            protected=protected,
            priority=priority,
        )

    def _entry_tokens(self, entry: ContextEntry) -> int:
        # 预留少量来源标签和分隔符开销，避免预算估算只计算正文。
        return max(1, self._estimate_content_tokens(entry.content)) + 8

    @staticmethod
    def _default_token_estimator(content: str) -> int:
        # 无第三方 tokenizer 时对中英文混合文本采用保守近似。
        return max(1, (len(content) + 1) // 2)

    @staticmethod
    def _terms(content: str) -> set[str]:
        normalized = content.casefold()
        terms = set(re.findall(r"[a-z0-9][a-z0-9_-]*", normalized))
        for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
            if len(segment) == 1:
                terms.add(segment)
            else:
                terms.update(segment[index : index + 2] for index in range(len(segment) - 1))
        return terms

    @staticmethod
    def _unique(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))
