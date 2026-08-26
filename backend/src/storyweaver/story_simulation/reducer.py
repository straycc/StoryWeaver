"""角色剧场沙盒状态归约；所有错误均阻止提交。"""

from __future__ import annotations

from dataclasses import replace

from .models import SimulationEvent, SimulationState, SimulationStateDelta


class SimulationStateReducer:
    def apply(self, *, state: SimulationState, delta: SimulationStateDelta, known_fact_ids: set[str], source_turn: int) -> SimulationState:
        characters = {item.character_id: item for item in state.characters}
        present = set(state.present_character_ids)
        for change in delta.character_changes:
            ident = str(change.get("character_id") or "")
            if ident not in characters or ident not in present:
                raise ValueError("只能更新当前在场人物")
            current = characters[ident]
            characters[ident] = replace(
                current,
                emotion=str(change.get("emotion", current.emotion)),
                current_intent=str(change.get("current_intent", current.current_intent)),
                attitude=str(change.get("attitude", current.attitude)),
            )
        for change in delta.knowledge_changes:
            character_id = str(change.get("character_id") or "")
            fact_id = str(change.get("fact_id") or "")
            if character_id not in characters or character_id not in present or fact_id not in known_fact_ids:
                raise ValueError("人物只能学习有来源的既有事实")
            current = characters[character_id]
            if fact_id not in current.known_fact_ids:
                characters[character_id] = replace(current, known_fact_ids=current.known_fact_ids + (fact_id,))
        for change in delta.character_presence_changes:
            ident = str(change.get("character_id") or "")
            action = str(change.get("action") or "")
            if ident not in characters:
                raise ValueError("只能变更场景参与人物的在场状态")
            if action == "enter":
                if ident in present:
                    raise ValueError("已在场人物不能重复进入")
                present.add(ident)
            elif action == "leave":
                if ident not in present:
                    raise ValueError("不在场人物不能离开")
                if len(present) == 1:
                    raise ValueError("场景至少需要保留一名在场人物")
                present.remove(ident)
            else:
                raise ValueError("人物在场变化必须是 enter 或 leave")
        events = list(state.active_events)
        for event in delta.event_changes:
            description = str(event.get("description") or "")
            if not description:
                raise ValueError("事件变化必须包含描述")
            events.append(SimulationEvent(
                event_id=f"sim-event-{source_turn}-{len(events)+1}", description=description,
                status=str(event.get("status", "active")), source_turn=source_turn,
                observable_by=tuple(str(item) for item in event.get("observable_by", present)),
            ))
        relationships = {self._relationship_key(item): dict(item) for item in state.relationships}
        for change in delta.relationship_changes:
            source = str(change.get("source_character_id") or change.get("from_character_id") or "")
            target = str(change.get("target_character_id") or change.get("to_character_id") or "")
            if source not in characters or target not in characters or source == target:
                raise ValueError("关系变化必须引用两个不同的剧场人物")
            normalized = dict(change)
            normalized["source_character_id"] = source
            normalized["target_character_id"] = target
            key = self._relationship_key(normalized)
            reason = str(normalized.get("reason") or "").strip()
            if not reason:
                raise ValueError("关系变化必须说明原因")
            # 关系演变是正常剧情状态；保留理由与来源回合，避免无审计覆盖。
            normalized["reason"] = reason
            normalized["source_turn"] = source_turn
            relationships[key] = normalized
        scene = self._apply_scene_update(state=state, update=delta.scene_update)
        return SimulationState(
            current_time=str(scene["current_time"]),
            current_location=str(scene["current_location"]),
            scene_summary=str(scene["scene_summary"]),
            active_event=delta.active_event if delta.active_event is not None else scene["active_event"],
            scene_status=str(scene["scene_status"]),
            present_character_ids=tuple(item.character_id for item in state.characters if item.character_id in present), characters=tuple(characters[item.character_id] for item in state.characters),
            relationships=tuple(relationships.values()), active_events=tuple(events), inventory=state.inventory,
        )

    @staticmethod
    def _relationship_key(value: dict[str, object]) -> str:
        source = str(value.get("source_character_id") or value.get("from_character_id") or "")
        target = str(value.get("target_character_id") or value.get("to_character_id") or "")
        relation_type = str(value.get("relation_type") or value.get("type") or "default")
        return f"{source}:{target}:{relation_type}"

    @staticmethod
    def _apply_scene_update(*, state: SimulationState,
                            update: dict[str, object] | None) -> dict[str, str | None]:
        """应用导演提出的有限舞台变化，不扩展为开放世界位置模拟。"""
        result: dict[str, str | None] = {
            "current_time": state.current_time,
            "current_location": state.current_location,
            "scene_summary": state.scene_summary,
            "active_event": state.active_event,
            "scene_status": state.scene_status,
        }
        if not update:
            return result
        allowed = {
            "current_time", "current_location", "scene_summary", "active_event",
            "scene_status", "transition_reason",
        }
        unknown = set(update) - allowed
        if unknown:
            raise ValueError(f"场景更新包含未知字段：{', '.join(sorted(unknown))}")
        for field in ("current_time", "scene_summary", "active_event"):
            value = update.get(field)
            if value is None:
                continue
            text = str(value).strip()
            if not text or len(text) > 500:
                raise ValueError(f"场景更新 {field} 不合法")
            result[field] = text
        location = update.get("current_location")
        if location is not None:
            reason = str(update.get("transition_reason") or "").strip()
            text = str(location).strip()
            if not reason or not text or len(text) > 300:
                raise ValueError("变更场景地点必须提供有效转场原因")
            result["current_location"] = text
        status = update.get("scene_status")
        if status is not None:
            if status not in {"ongoing", "paused", "completed"}:
                raise ValueError("场景状态不合法")
            result["scene_status"] = str(status)
        return result
