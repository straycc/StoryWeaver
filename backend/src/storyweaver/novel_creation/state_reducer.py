"""把经过校验的章节状态增量归约为新的权威状态。"""

from __future__ import annotations

from dataclasses import replace

from .exceptions import StateTransitionError
from .models import (
    CharacterState,
    CharacterStateUpdate,
    FactRecord,
    StoryHook,
    StoryState,
    StoryStateDelta,
)


class NovelStateReducer:
    """小说状态的唯一确定性修改入口。"""

    _ALLOWED_HOOK_TRANSITIONS = {
        "open": frozenset({"open", "progressing", "deferred", "resolved"}),
        "progressing": frozenset({"progressing", "deferred", "resolved"}),
        "deferred": frozenset({"deferred", "progressing", "resolved"}),
        "resolved": frozenset({"resolved"}),
    }

    def apply(self, state: StoryState, delta: StoryStateDelta) -> StoryState:
        """校验并应用一章增量，返回全新的 ``StoryState``。"""

        expected_chapter = state.last_committed_chapter + 1
        if delta.source_chapter != expected_chapter:
            raise StateTransitionError(
                "状态增量章节号不连续："
                f"期望 {expected_chapter}，实际 {delta.source_chapter}"
            )

        invalidated_ids = self._validate_invalidated_facts(state, delta)
        facts, fact_aliases = self._reduce_facts(
            state=state,
            delta=delta,
            invalidated_ids=invalidated_ids,
        )
        characters = self._reduce_characters(
            state=state,
            delta=delta,
            facts=facts,
            fact_aliases=fact_aliases,
        )
        hooks = self._reduce_hooks(state=state, delta=delta)

        return StoryState(
            schema_version=state.schema_version,
            book_id=state.book_id,
            last_committed_chapter=delta.source_chapter,
            current_time=delta.new_time or state.current_time,
            current_location=delta.new_location or state.current_location,
            characters=characters,
            facts=facts,
            hooks=hooks,
        )

    def _validate_invalidated_facts(
        self,
        state: StoryState,
        delta: StoryStateDelta,
    ) -> frozenset[str]:
        current_fact_ids = {fact.fact_id for fact in state.current_facts}
        invalidated_ids = frozenset(delta.invalidated_fact_ids)
        unknown_ids = invalidated_ids - current_fact_ids
        if unknown_ids:
            names = ", ".join(sorted(unknown_ids))
            raise StateTransitionError(f"不能使不存在或已失效的事实失效：{names}")
        return invalidated_ids

    def _reduce_facts(
        self,
        *,
        state: StoryState,
        delta: StoryStateDelta,
        invalidated_ids: frozenset[str],
    ) -> tuple[tuple[FactRecord, ...], dict[str, str]]:
        existing_ids = {fact.fact_id for fact in state.facts}
        active_signatures = {
            self._fact_signature(fact): fact.fact_id
            for fact in state.current_facts
            if fact.fact_id not in invalidated_ids
        }

        reduced_facts = [
            replace(fact, valid_until_chapter=delta.source_chapter)
            if fact.fact_id in invalidated_ids
            else fact
            for fact in state.facts
        ]
        aliases: dict[str, str] = {}

        for fact in delta.new_facts:
            if fact.fact_id in existing_ids:
                raise StateTransitionError(f"事实 ID 已存在：{fact.fact_id}")
            if fact.source_chapter != delta.source_chapter:
                raise StateTransitionError(
                    f"事实 {fact.fact_id} 的 source_chapter 与增量章节不一致"
                )
            if fact.valid_from_chapter != delta.source_chapter:
                raise StateTransitionError(
                    f"新事实 {fact.fact_id} 必须从来源章节开始生效"
                )
            if fact.valid_until_chapter is not None:
                raise StateTransitionError(f"新事实 {fact.fact_id} 不能预先失效")

            signature = self._fact_signature(fact)
            canonical_id = active_signatures.get(signature)
            if canonical_id is not None:
                # 重复事实不追加，但保留 ID 映射，角色学习记录仍能落到已有事实。
                aliases[fact.fact_id] = canonical_id
                continue

            reduced_facts.append(fact)
            existing_ids.add(fact.fact_id)
            active_signatures[signature] = fact.fact_id

        return tuple(reduced_facts), aliases

    def _reduce_characters(
        self,
        *,
        state: StoryState,
        delta: StoryStateDelta,
        facts: tuple[FactRecord, ...],
        fact_aliases: dict[str, str],
    ) -> tuple[CharacterState, ...]:
        updates = {update.character_id: update for update in delta.character_updates}
        character_ids = {character.character_id for character in state.characters}
        unknown_ids = set(updates) - character_ids
        if unknown_ids:
            names = ", ".join(sorted(unknown_ids))
            raise StateTransitionError(f"不能更新不存在的角色：{names}")

        valid_fact_ids = {fact.fact_id for fact in facts}
        reduced_characters: list[CharacterState] = []
        for character in state.characters:
            update = updates.get(character.character_id)
            if update is None:
                reduced_characters.append(character)
                continue
            reduced_characters.append(
                self._apply_character_update(
                    character=character,
                    update=update,
                    valid_fact_ids=valid_fact_ids,
                    fact_aliases=fact_aliases,
                )
            )
        return tuple(reduced_characters)

    def _apply_character_update(
        self,
        *,
        character: CharacterState,
        update: CharacterStateUpdate,
        valid_fact_ids: set[str],
        fact_aliases: dict[str, str],
    ) -> CharacterState:
        additions = set(update.add_possessions)
        removals = set(update.remove_possessions)
        conflicts = additions & removals
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise StateTransitionError(f"物品不能在同一增量中同时新增和移除：{names}")

        missing_possessions = removals - set(character.possessions)
        if missing_possessions:
            names = ", ".join(sorted(missing_possessions))
            raise StateTransitionError(
                f"角色 {character.character_id} 不能移除未持有的物品：{names}"
            )

        possessions = [
            item for item in character.possessions if item not in removals
        ]
        for item in update.add_possessions:
            if item not in possessions:
                possessions.append(item)

        learned_fact_ids = tuple(
            fact_aliases.get(fact_id, fact_id) for fact_id in update.learned_fact_ids
        )
        unknown_facts = set(learned_fact_ids) - valid_fact_ids
        if unknown_facts:
            names = ", ".join(sorted(unknown_facts))
            raise StateTransitionError(
                f"角色 {character.character_id} 不能学习不存在的事实：{names}"
            )

        known_fact_ids = list(character.known_fact_ids)
        for fact_id in learned_fact_ids:
            if fact_id not in known_fact_ids:
                known_fact_ids.append(fact_id)

        return CharacterState(
            character_id=character.character_id,
            location=update.location or character.location,
            status=update.status or character.status,
            current_goal=update.current_goal or character.current_goal,
            emotion=update.emotion or character.emotion,
            possessions=tuple(possessions),
            known_fact_ids=tuple(known_fact_ids),
        )

    def _reduce_hooks(
        self,
        *,
        state: StoryState,
        delta: StoryStateDelta,
    ) -> tuple[StoryHook, ...]:
        hooks = {hook.hook_id: hook for hook in state.hooks}
        new_hook_ids = {hook.hook_id for hook in delta.new_hooks}
        existing_collisions = new_hook_ids & set(hooks)
        if existing_collisions:
            names = ", ".join(sorted(existing_collisions))
            raise StateTransitionError(f"伏笔 ID 已存在：{names}")

        update_ids = {update.hook_id for update in delta.hook_updates}
        ambiguous_ids = new_hook_ids & update_ids
        if ambiguous_ids:
            names = ", ".join(sorted(ambiguous_ids))
            raise StateTransitionError(f"新伏笔不能在同一增量中再次更新：{names}")

        for hook in delta.new_hooks:
            if hook.opened_chapter != delta.source_chapter:
                raise StateTransitionError(
                    f"新伏笔 {hook.hook_id} 的 opened_chapter 必须等于来源章节"
                )
            if hook.last_advanced_chapter != delta.source_chapter:
                raise StateTransitionError(
                    f"新伏笔 {hook.hook_id} 的 last_advanced_chapter 必须等于来源章节"
                )
            hooks[hook.hook_id] = hook

        unknown_updates = update_ids - set(hooks)
        if unknown_updates:
            names = ", ".join(sorted(unknown_updates))
            raise StateTransitionError(f"不能更新不存在的伏笔：{names}")

        for update in delta.hook_updates:
            current = hooks[update.hook_id]
            allowed = self._ALLOWED_HOOK_TRANSITIONS[current.status]
            if update.status not in allowed:
                raise StateTransitionError(
                    f"伏笔 {current.hook_id} 不能从 {current.status} 回退到 "
                    f"{update.status}"
                )
            hooks[current.hook_id] = replace(
                current,
                status=update.status,
                last_advanced_chapter=delta.source_chapter,
            )

        # 字典保持原伏笔顺序，并在尾部追加新伏笔。
        return tuple(hooks.values())

    @staticmethod
    def _fact_signature(fact: FactRecord) -> tuple[str, str, str]:
        return (
            NovelStateReducer._normalize_text(fact.subject_id),
            NovelStateReducer._normalize_text(fact.predicate),
            NovelStateReducer._normalize_text(fact.value),
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.casefold().split())
