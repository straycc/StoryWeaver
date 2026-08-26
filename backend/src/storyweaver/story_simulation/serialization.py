"""角色剧场 JSON 编解码。"""

from __future__ import annotations

from typing import Any

from .models import (SimulationCharacter, SimulationCharacterState, SimulationEvent,
                     SimulationSession, SimulationState, SimulationTurn)


def state_from_data(data: dict[str, Any]) -> SimulationState:
    return SimulationState(
        current_time=str(data["current_time"]), current_location=str(data["current_location"]),
        # 兼容早期本地沙盒；新建模拟统一使用 scene_summary/opening_direction。
        scene_summary=str(data.get("scene_summary") or data.get("scene_goal") or "人物在当前场景中互动"),
        active_event=str(data["active_event"]) if data.get("active_event") else None,
        scene_status=str(data.get("scene_status", "ongoing")),
        present_character_ids=tuple(data["present_character_ids"]),
        characters=tuple(SimulationCharacterState(
            character_id=str(item["character_id"]), emotion=str(item["emotion"]),
            current_intent=str(item["current_intent"]),
            attitude=str(item["attitude"]), known_fact_ids=tuple(item.get("known_fact_ids", ())),
            concealed_fact_ids=tuple(item.get("concealed_fact_ids", ())), possessions=tuple(item.get("possessions", ())),
        ) for item in data.get("characters", ())),
        relationships=tuple(dict(item) for item in data.get("relationships", ())),
        active_events=tuple(SimulationEvent(
            event_id=str(item["event_id"]), description=str(item["description"]),
            status=str(item.get("status", "active")), source_turn=int(item.get("source_turn", 0)),
            observable_by=tuple(item.get("observable_by", ())),
        ) for item in data.get("active_events", ())),
        inventory=tuple(dict(item) for item in data.get("inventory", ())),
    )


def session_from_row(row: Any) -> SimulationSession:
    return SimulationSession(
        simulation_id=row.simulation_id, book_id=row.book_id, base_book_version=int(row.base_book_version),
        base_chapter_number=int(row.base_chapter_number), mode=row.mode, user_character_id=row.user_character_id,
        status=row.status, current_turn=int(row.current_turn), version=int(row.version),
        base_snapshot=dict(row.base_snapshot_json), scene_config=dict(row.scene_config_json),
        current_state=state_from_data(dict(row.current_state_json)), created_at=row.created_at, updated_at=row.updated_at,
    )


def turn_from_row(row: Any) -> SimulationTurn:
    return SimulationTurn(
        turn_id=row.turn_id, simulation_id=row.simulation_id, turn_number=int(row.turn_number), job_id=row.job_id,
        client_request_id=row.client_request_id, mode=row.mode, user_character_id=row.user_character_id,
        target_character_id=getattr(row, "target_character_id", None),
        input_type=row.input_type, user_input=row.user_input, status=row.status,
        output=dict(row.output_json) if row.output_json else None,
        state_delta=dict(row.state_delta_json) if row.state_delta_json else None,
        state_before_hash=row.state_before_hash, state_after_hash=row.state_after_hash,
        context_snapshot_id=row.context_snapshot_id, created_at=row.created_at,
    )


def characters_from_snapshot(value: object) -> tuple[SimulationCharacter, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(SimulationCharacter(**dict(item)) for item in value if isinstance(item, dict))
