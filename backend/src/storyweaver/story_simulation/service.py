"""角色剧场应用服务。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from ..persistence.context_snapshots import ContextSnapshotRepository
from ..novel_creation.repository import StoryProjectRepository
from ..persistence.simulations import SimulationRepository
from ..novel_creation.models import StoryState
from .runtime import RoleplayRuntime
from .models import SimulationCharacter, SimulationCharacterState, SimulationSession, SimulationState, SimulationStateDelta
from .reducer import SimulationStateReducer


class RoleplayService:
    def __init__(self, *, projects: StoryProjectRepository, simulations: SimulationRepository,
                 runtime: RoleplayRuntime, snapshots: ContextSnapshotRepository) -> None:
        self.projects = projects; self.simulations = simulations; self.runtime = runtime; self.snapshots = snapshots
        self.reducer = SimulationStateReducer()

    def create(self, *, book_id: str, base_chapter_number: int, mode: str, user_character_id: str | None,
               canonical_character_ids: tuple[str, ...], custom_characters: tuple[dict[str, str], ...],
               location: str, opening_direction: str) -> SimulationSession:
        project, book_version = self.projects.load_project_with_version(book_id)
        if base_chapter_number < 1 or base_chapter_number > project.state.last_committed_chapter:
            raise ValueError("基准章节必须是已提交章节")
        base_state = self.projects.load_snapshot(book_id, base_chapter_number)
        location = location.strip() or base_state.current_location
        opening_direction = opening_direction.strip() or "人物在当前局势下通过对话与行动自然推进场景。"
        profiles = {item.character_id: item for item in project.foundation.characters}
        selected = tuple(dict.fromkeys(canonical_character_ids))
        if len(selected) + len(custom_characters) > 6 or not selected and not custom_characters:
            raise ValueError("场景参与人物必须为 1 至 6 名")
        characters: list[SimulationCharacter] = []
        state_by_id = {item.character_id: item for item in base_state.characters}
        for ident in selected:
            profile = profiles.get(ident); state = state_by_id.get(ident)
            if profile is None or state is None:
                raise ValueError(f"基准章节不存在人物：{ident}")
            characters.append(SimulationCharacter(ident, profile.name, "canonical", profile.role, state.current_goal, "；".join(profile.knowledge_boundaries) or "无", profile.motivation, profile.speech_style))
        for item in custom_characters:
            ident = str(item.get("character_id") or f"sandbox-{uuid4().hex[:10]}")
            characters.append(SimulationCharacter(ident, str(item["name"]), str(item.get("origin") or "sandbox_npc"), str(item["role"]), str(item["goal"]), str(item.get("secret") or "无"), str(item.get("public_profile") or item["role"])))
        ids = tuple(item.character_id for item in characters)
        if mode == "roleplay" and user_character_id not in ids:
            raise ValueError("用户扮演人物必须是场景参与者")
        initial_characters = []
        for character in characters:
            state = state_by_id.get(character.character_id)
            initial_characters.append(SimulationCharacterState(character.character_id, state.emotion if state else "平静", state.current_goal if state else character.goal, "中立", state.known_fact_ids if state else ()))
        snapshot = {"foundation": asdict(project.foundation), "state": asdict(base_state), "characters": [asdict(item) for item in characters], "writing_rules": list(project.foundation.writing_rules), "fact_ids": [item.fact_id for item in base_state.current_facts]}
        now = datetime.now(timezone.utc).isoformat()
        return self.simulations.create(SimulationSession(str(uuid4()), book_id, book_version, base_chapter_number, mode, user_character_id, "active", 0, 1, snapshot, {"location": location, "opening_direction": opening_direction}, SimulationState(base_state.current_time, location, opening_direction, None, "ongoing", ids, tuple(initial_characters)), now, now))

    async def run_turn(self, *, turn_id: str, job_id: str, emit: Callable[[str, dict[str, Any]], None]) -> SimulationSession:
        turn = self.simulations.start_turn(turn_id); simulation = self.simulations.get(turn.simulation_id)
        emit("simulation_context_ready", {"simulation_id": simulation.simulation_id, "turn_number": turn.turn_number})
        try:
            result = await self.runtime.run_turn(
                simulation=simulation, turn=turn, job_id=job_id,
                history=self.simulations.list_turns(simulation.simulation_id), emit=emit,
            )
            output = result.output
            blocks = [item.model_dump() for item in output.blocks]
            delta = result.delta
            state = self.reducer.apply(state=simulation.current_state, delta=delta, known_fact_ids=set(simulation.base_snapshot.get("fact_ids", [])), source_turn=turn.turn_number)
            # scene_update 可推进场景状态；旧字段仅在导演显式要求暂停/完成时兼容覆盖。
            scene_status = state.scene_status
            if output.scene_status != "ongoing":
                scene_status = output.scene_status
            # 用户扮演角色离开舞台后，等待用户以新的场景输入继续，不让模型代替其行动。
            if simulation.mode == "roleplay" and simulation.user_character_id not in state.present_character_ids:
                scene_status = "paused"
            state = SimulationState(state.current_time, state.current_location, state.scene_summary, state.active_event, scene_status, state.present_character_ids, state.characters, state.relationships, state.active_events, state.inventory)
            persisted = {"blocks": blocks, "suggested_actions": output.suggested_actions[:4], "scene_status": output.scene_status, "turn_summary": output.turn_summary}
            primary_snapshot = result.snapshot_links[-1][1] if result.snapshot_links else None
            committed = self.simulations.commit_turn(
                turn_id=turn_id, expected_version=simulation.version, state=state,
                output=persisted, delta=asdict(delta), context_snapshot_id=primary_snapshot,
                snapshot_links=result.snapshot_links,
            )
            emit("simulation_model_completed", {"turn_number": turn.turn_number, "agent": "roleplay_runtime"})
            for block in blocks: emit("simulation_message", {"turn_number": turn.turn_number, "block": block})
            emit("simulation_state_changed", {"turn_number": turn.turn_number, "state": public_state(committed)})
            emit("simulation_turn_committed", {"turn_number": turn.turn_number, "simulation_version": committed.version})
            return committed
        except Exception:
            self.simulations.fail_turn(turn_id)
            emit("simulation_turn_failed", {"turn_number": turn.turn_number, "discard_preview": True})
            raise

    def change_mode(self, **kwargs: Any) -> SimulationSession:
        return self.simulations.update_mode(**kwargs)


def public_state(simulation: SimulationSession) -> dict[str, object]:
    names = {str(item["character_id"]): str(item["name"]) for item in simulation.base_snapshot.get("characters", []) if isinstance(item, dict)}
    present = set(simulation.current_state.present_character_ids)
    return {"simulation_id": simulation.simulation_id, "version": simulation.version, "status": simulation.status, "mode": simulation.mode, "user_character_id": simulation.user_character_id, "current_turn": simulation.current_turn, "current_time": simulation.current_state.current_time, "current_location": simulation.current_state.current_location, "scene_summary": simulation.current_state.scene_summary, "active_event": simulation.current_state.active_event, "scene_status": simulation.current_state.scene_status, "characters": [{"character_id": item.character_id, "name": names.get(item.character_id, item.character_id), "emotion": item.emotion, "attitude": item.attitude, "present": item.character_id in present} for item in simulation.current_state.characters]}
