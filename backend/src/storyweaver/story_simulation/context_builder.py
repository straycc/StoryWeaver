"""角色剧场的 Director/Character 专属 Context 视图。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..context import ContextCandidate, select_context
from .models import SimulationCharacter, SimulationSession, SimulationTurn


class RoleplayContextBuilder:
    """共享预算与 Trace，绝不共享角色私有内容。"""

    director_policy_version = "roleplay-director-context-v2"
    character_policy_version = "roleplay-character-context-v2"

    def build_director(self, *, simulation: SimulationSession, turns: tuple[SimulationTurn, ...],
                       request: str, input_type: str, candidate_ids: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
        profiles = self._characters(simulation)
        public_profiles = [self._public_profile(item) for item in profiles if item.character_id in candidate_ids]
        candidates = self._base_candidates(
            simulation=simulation, request=request, input_type=input_type,
            scene_content=json.dumps(self._public_scene(simulation), ensure_ascii=False),
            character_content=json.dumps(public_profiles, ensure_ascii=False),
        )
        candidates.extend(self._recent_turn_candidates(turns))
        return self._render(
            simulation=simulation, candidates=candidates, agent_role="scene_director",
            policy_version=self.director_policy_version,
            notes=(f"candidate_ids={','.join(candidate_ids)}", "角色秘密未提供给导演"),
        )

    def build_character(self, *, simulation: SimulationSession, turns: tuple[SimulationTurn, ...], request: str,
                        input_type: str, character_id: str, scene_instruction: str,
                        visible_contributions: tuple[dict[str, Any], ...]) -> tuple[str, dict[str, Any]]:
        profiles = self._characters(simulation)
        current = next((item for item in profiles if item.character_id == character_id), None)
        if current is None:
            raise ValueError("角色剧场缺少指定人物")
        others = [self._public_profile(item) for item in profiles if item.character_id != character_id]
        own_state = next((item for item in simulation.current_state.characters if item.character_id == character_id), None)
        own = {
            "profile": asdict(current),
            "state": asdict(own_state) if own_state else {},
            "known_fact_ids": list(own_state.known_fact_ids) if own_state else [],
        }
        candidates = self._base_candidates(
            simulation=simulation, request=request, input_type=input_type,
            scene_content=json.dumps({
                "scene": self._public_scene(simulation),
                "director_instruction": scene_instruction,
                "visible_contributions": list(visible_contributions),
            }, ensure_ascii=False),
            character_content=json.dumps({"self": own, "other_public_profiles": others}, ensure_ascii=False),
        )
        candidates.extend(self._recent_turn_candidates(turns))
        return self._render(
            simulation=simulation, candidates=candidates, agent_role="character_agent",
            policy_version=self.character_policy_version,
            notes=(f"character_id={character_id}", "仅该角色私有资料可见"),
        )

    def _base_candidates(self, *, simulation: SimulationSession, request: str, input_type: str,
                         scene_content: str, character_content: str) -> list[ContextCandidate]:
        snapshot = simulation.base_snapshot
        items = [
            ("request", request, "本轮输入"),
            ("mode", json.dumps({"mode": simulation.mode, "user_character_id": simulation.user_character_id, "input_type": input_type}, ensure_ascii=False), "互动限制"),
            ("scene", scene_content, "当前场景"),
            ("characters", character_content, "人物资料"),
            ("rules", json.dumps(snapshot.get("writing_rules", []), ensure_ascii=False), "世界硬规则"),
        ]
        return [
            ContextCandidate(
                source_id=f"simulation:{simulation.simulation_id}:{ident}",
                source_type="simulation",
                content=content, reason=reason, protected=True, priority=100, digest=content[:1200],
            )
            for ident, content, reason in items
        ]

    def _recent_turn_candidates(self, turns: tuple[SimulationTurn, ...]) -> list[ContextCandidate]:
        values: list[ContextCandidate] = []
        for turn in turns[-8:]:
            if not turn.output:
                continue
            public = {
                "turn_number": turn.turn_number,
                "input_type": turn.input_type,
                "user_input": turn.user_input,
                "blocks": turn.output.get("blocks", []),
                "turn_summary": turn.output.get("turn_summary", ""),
            }
            content = json.dumps(public, ensure_ascii=False)
            values.append(ContextCandidate(
                source_id=f"simulation-turn:{turn.turn_number}",
                source_type="simulation_turn",
                content=content, reason="最近已提交回合", protected=False, priority=80,
            ))
        return values

    def _render(self, *, simulation: SimulationSession, candidates: list[ContextCandidate], agent_role: str,
                policy_version: str, notes: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
        selected, trace = select_context(
            agent_role=agent_role, policy_version=policy_version, book_version=simulation.base_book_version,
            token_budget=5000, candidates=tuple(candidates),
            notes=(f"simulation_id={simulation.simulation_id}", f"base_chapter={simulation.base_chapter_number}", *notes),
        )
        rendered = "\n\n".join(f"## {item.reason}\n{item.content}" for item in selected)
        return rendered, trace.to_data()

    @staticmethod
    def _characters(simulation: SimulationSession) -> tuple[SimulationCharacter, ...]:
        raw = simulation.base_snapshot.get("characters", [])
        return tuple(SimulationCharacter(**dict(item)) for item in raw if isinstance(item, dict))

    @staticmethod
    def _public_profile(character: SimulationCharacter) -> dict[str, str]:
        return {
            "character_id": character.character_id,
            "name": character.name,
            "role": character.role,
            "public_profile": character.public_profile,
            "speech_style": character.speech_style,
        }

    @staticmethod
    def _public_scene(simulation: SimulationSession) -> dict[str, Any]:
        """只投影所有角色可观察到的舞台信息，绝不泄漏知识或秘密。"""
        state = simulation.current_state
        present = set(state.present_character_ids)
        return {
            "current_time": state.current_time,
            "current_location": state.current_location,
            "scene_summary": state.scene_summary,
            "active_event": state.active_event,
            "scene_status": state.scene_status,
            "present_character_ids": list(state.present_character_ids),
            "characters": [
                {
                    "character_id": item.character_id,
                    "emotion": item.emotion,
                    "attitude": item.attitude,
                    "present": item.character_id in present,
                }
                for item in state.characters
            ],
            "active_events": [
                {"event_id": event.event_id, "description": event.description, "status": event.status}
                for event in state.active_events
                if not event.observable_by or present.issubset(set(event.observable_by))
            ],
        }
