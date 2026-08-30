"""角色剧场 V2 回合编排：路由、导演、角色贡献与组装。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..persistence.context_snapshots import ContextSnapshotRepository
from ..skills import CreativeTaskContext, SkillMaterializer
from .agent import (CharacterAgent, CharacterContributionOutput, RoleplayDeltaOutput,
                    RoleplayTurnOutput, SceneDirectorAgent, TurnPlanOutput)
from .context_builder import RoleplayContextBuilder
from .models import SimulationSession, SimulationStateDelta, SimulationTurn


@dataclass(frozen=True, slots=True)
class RouteDecision:
    path: str  # direct_character | director
    actor_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    output: RoleplayTurnOutput
    delta: SimulationStateDelta
    snapshot_links: tuple[tuple[str, str, str | None, int], ...]


class TurnRouter:
    """确定性选择低成本直达路径或 Director 路径。"""

    def route(self, *, simulation: SimulationSession, turn: SimulationTurn) -> RouteDecision:
        present = tuple(simulation.current_state.present_character_ids)
        controllable = tuple(item for item in present if item != simulation.user_character_id)
        if not controllable:
            raise ValueError("当前场景没有可由系统控制的 NPC")
        if turn.input_type in {"director_event", "observer_continue"}:
            return RouteDecision("director", (), "导演事件或自动旁观必须由导演规划")
        if turn.target_character_id:
            if turn.target_character_id not in controllable:
                raise ValueError("目标人物必须是当前在场的 NPC")
            return RouteDecision("direct_character", (turn.target_character_id,), "用户明确选择目标人物")
        if len(controllable) == 1:
            return RouteDecision("direct_character", controllable, "当前只有一名可控 NPC")
        matched = self._match_unique_name(simulation, turn.user_input, controllable)
        if matched:
            return RouteDecision("direct_character", (matched,), "文本唯一命中 NPC 名称")
        return RouteDecision("director", (), "输入未指定唯一目标，交给场景导演")

    @staticmethod
    def _match_unique_name(simulation: SimulationSession, text: str, candidates: tuple[str, ...]) -> str | None:
        raw = simulation.base_snapshot.get("characters", [])
        names = {
            str(item.get("character_id")): str(item.get("name"))
            for item in raw if isinstance(item, dict) and str(item.get("character_id")) in candidates
        }
        matched = [ident for ident, name in names.items() if name and name in text]
        return matched[0] if len(matched) == 1 else None


class TurnAssembler:
    """把角色独立贡献转为唯一的已提交回合；不静默覆盖冲突。"""

    def assemble(self, *, simulation: SimulationSession, actor_ids: tuple[str, ...],
                 contributions: tuple[CharacterContributionOutput, ...], plan: TurnPlanOutput | None) -> tuple[RoleplayTurnOutput, SimulationStateDelta]:
        if len(actor_ids) != len(contributions):
            raise ValueError("角色贡献数量与导演计划不一致")
        blocks: list[dict[str, Any]] = []
        character_changes: list[dict[str, Any]] = []
        relationship_changes: list[dict[str, Any]] = []
        knowledge_changes: list[dict[str, Any]] = []
        event_changes: list[dict[str, Any]] = []
        suggested: list[str] = []
        summaries: list[str] = []
        seen_relationships: set[str] = set()
        for actor_id, contribution in zip(actor_ids, contributions, strict=True):
            payload = contribution.proposed_delta
            if payload.character_presence_changes:
                raise ValueError("CharacterAgent 不得直接安排人物出入场")
            for block in contribution.blocks:
                data = block.model_dump()
                if data.get("type") == "dialogue" and data.get("speaker_id") != actor_id:
                    raise ValueError("CharacterAgent 只能让被分配角色发言")
                blocks.append(data)
            self._validate_self_delta(actor_id, payload)
            character_changes.extend(payload.character_changes)
            knowledge_changes.extend(payload.knowledge_changes)
            event_changes.extend(payload.event_changes)
            for change in payload.relationship_changes:
                key = self._relationship_key(change)
                if key in seen_relationships:
                    raise ValueError("同一回合不能重复提交同一关系变化")
                seen_relationships.add(key)
                relationship_changes.append(change)
            suggested.extend(item for item in contribution.suggested_actions if item and item not in suggested)
            if contribution.observable_summary:
                summaries.append(contribution.observable_summary)
        plan_delta = plan or TurnPlanOutput(actor_ids=list(actor_ids), scene_instruction="直接回应用户输入")
        delta = SimulationStateDelta(
            character_changes=tuple(character_changes), relationship_changes=tuple(relationship_changes),
            knowledge_changes=tuple(knowledge_changes), event_changes=tuple(event_changes),
            character_presence_changes=tuple(plan_delta.character_presence_changes), active_event=plan_delta.active_event,
            scene_update=dict(plan_delta.scene_update) if plan_delta.scene_update else None,
        )
        output = RoleplayTurnOutput(
            blocks=blocks, proposed_delta=RoleplayDeltaOutput(**asdict(delta)),
            suggested_actions=suggested[:4], scene_status=plan_delta.scene_status,
            turn_summary="；".join(summaries)[:800],
        )
        return output, delta

    @staticmethod
    def _validate_self_delta(actor_id: str, delta: RoleplayDeltaOutput) -> None:
        if delta.event_changes or delta.character_presence_changes or delta.active_event or delta.scene_update:
            raise ValueError("CharacterAgent 不得直接修改场景状态")
        for change in [*delta.character_changes, *delta.knowledge_changes]:
            if str(change.get("character_id") or "") != actor_id:
                raise ValueError("角色只能提交自身状态或知识变化")
        for change in delta.relationship_changes:
            source = str(change.get("source_character_id") or change.get("from_character_id") or "")
            if source != actor_id:
                raise ValueError("角色只能提出自己发起的关系变化")

    @staticmethod
    def _relationship_key(change: dict[str, Any]) -> str:
        source = str(change.get("source_character_id") or change.get("from_character_id") or "")
        target = str(change.get("target_character_id") or change.get("to_character_id") or "")
        relation_type = str(change.get("relation_type") or change.get("type") or "default")
        return f"{source}:{target}:{relation_type}"


class RoleplayRuntime:
    def __init__(self, *, director: SceneDirectorAgent, character: CharacterAgent,
                 snapshots: ContextSnapshotRepository,
                 skill_materializer: SkillMaterializer | None = None) -> None:
        self._director = director
        self._character = character
        self._snapshots = snapshots
        self._router = TurnRouter()
        self._contexts = RoleplayContextBuilder()
        self._assembler = TurnAssembler()
        self._skill_materializer = skill_materializer

    async def run_turn(self, *, simulation: SimulationSession, turn: SimulationTurn, job_id: str,
                       history: tuple[SimulationTurn, ...],
                       creative_task: CreativeTaskContext | None = None,
                       emit: Callable[[str, dict[str, Any]], None]) -> RuntimeResult:
        decision = self._router.route(simulation=simulation, turn=turn)
        emit("simulation_router_selected", {"turn_number": turn.turn_number, "path": decision.path,
                                             "actor_ids": list(decision.actor_ids), "reason": decision.reason})
        links: list[tuple[str, str, str | None, int]] = []
        plan: TurnPlanOutput | None = None
        if decision.path == "director":
            candidates = tuple(item for item in simulation.current_state.present_character_ids if item != simulation.user_character_id)
            prompt, trace = self._contexts.build_director(
                simulation=simulation, turns=history, request=turn.user_input,
                input_type=turn.input_type, candidate_ids=candidates,
            )
            prompt = await self._with_skills(
                prompt,
                creative_task=creative_task,
                objective="根据当前场景和用户输入规划下一步叙事行动",
                emit=emit,
                worker_id="scene_director",
            )
            snapshot = self._snapshots.save(
                agent_role="scene_director", book_id=simulation.book_id, book_version=simulation.base_book_version,
                policy_version=self._contexts.director_policy_version, renderer_version="roleplay-director-v2",
                rendered_context=prompt, trace=trace, job_id=job_id, simulation_id=simulation.simulation_id,
            )
            links.append(("director", snapshot.snapshot_id, None, 0))
            emit("simulation_model_started", {"turn_number": turn.turn_number, "agent": "scene_director"})
            plan = await self._director.run(prompt)
            used_actor_fallback = False
            if not plan.actor_ids:
                # 模型有时只返回场景更新；当前候选顺序已由场景在场顺序冻结，补全可复现。
                if not candidates:
                    raise ValueError("当前场景没有可供导演安排的 NPC")
                plan = plan.model_copy(update={"actor_ids": [candidates[0]]})
                used_actor_fallback = True
                emit("simulation_director_planned", {
                    "turn_number": turn.turn_number,
                    "actor_ids": plan.actor_ids,
                    "scene_instruction": plan.scene_instruction,
                    "fallback": "director_missing_actor_ids",
                })
            self._validate_plan(simulation=simulation, plan=plan)
            decision = RouteDecision("director", tuple(plan.actor_ids), "导演已选择行动人物")
            if not used_actor_fallback:
                emit("simulation_director_planned", {"turn_number": turn.turn_number, "actor_ids": plan.actor_ids,
                                                       "scene_instruction": plan.scene_instruction})
        visible: list[dict[str, Any]] = []
        contributions: list[CharacterContributionOutput] = []
        for ordinal, actor_id in enumerate(decision.actor_ids, start=1):
            instruction = plan.scene_instruction if plan else "直接回应用户输入，不扩展为多人场景。"
            prompt, trace = self._contexts.build_character(
                simulation=simulation, turns=history, request=turn.user_input, input_type=turn.input_type,
                character_id=actor_id, scene_instruction=instruction, visible_contributions=tuple(visible),
            )
            prompt = await self._with_skills(
                prompt,
                creative_task=creative_task,
                objective=f"以角色 {actor_id} 的身份生成当前场景贡献",
                emit=emit,
                worker_id="character_agent",
            )
            snapshot = self._snapshots.save(
                agent_role="character_agent", book_id=simulation.book_id, book_version=simulation.base_book_version,
                policy_version=self._contexts.character_policy_version, renderer_version="roleplay-character-v2",
                rendered_context=prompt, trace=trace, job_id=job_id, simulation_id=simulation.simulation_id,
            )
            links.append(("character", snapshot.snapshot_id, actor_id, ordinal))
            emit("simulation_character_started", {"turn_number": turn.turn_number, "character_id": actor_id, "ordinal": ordinal})
            contribution = await self._character.run(prompt)
            self._assembler._validate_self_delta(actor_id, contribution.proposed_delta)
            for block in contribution.blocks:
                data = block.model_dump()
                if data.get("type") == "dialogue" and data.get("speaker_id") != actor_id:
                    raise ValueError("角色只能输出自己的台词")
                visible.append(data)
                emit("simulation_character_preview", {"turn_number": turn.turn_number, "character_id": actor_id,
                                                        "ordinal": ordinal, "block": data, "provisional": True})
            contributions.append(contribution)
        output, delta = self._assembler.assemble(
            simulation=simulation, actor_ids=decision.actor_ids, contributions=tuple(contributions), plan=plan,
        )
        return RuntimeResult(output=output, delta=delta, snapshot_links=tuple(links))

    async def _with_skills(
        self,
        prompt: str,
        *,
        creative_task: CreativeTaskContext | None,
        objective: str,
        emit: Callable[[str, dict[str, Any]], None],
        worker_id: str,
    ) -> str:
        if creative_task is None or not creative_task.applied_skills:
            return prompt
        if self._skill_materializer is None:
            raise RuntimeError("角色剧场未配置 SkillMaterializer")
        materialized = await self._skill_materializer.materialize(
            creative_task,
            objective=objective,
        )
        emit(
            "skill_resolved",
            {
                "agent_id": worker_id,
                **self._skill_materializer.observation(creative_task, materialized),
            },
        )
        return f"{prompt}\n\n{materialized.render()}"

    @staticmethod
    def _validate_plan(*, simulation: SimulationSession, plan: TurnPlanOutput) -> None:
        allowed = set(simulation.current_state.present_character_ids)
        if simulation.user_character_id:
            allowed.discard(simulation.user_character_id)
        if not set(plan.actor_ids).issubset(allowed) or len(set(plan.actor_ids)) != len(plan.actor_ids):
            raise ValueError("导演只能选择当前在场且不属于用户的 NPC，且不得重复")
        cast = {str(item.get("character_id")) for item in simulation.base_snapshot.get("characters", []) if isinstance(item, dict)}
        for change in plan.character_presence_changes:
            if str(change.get("character_id") or "") not in cast:
                raise ValueError("导演只能安排剧场已有角色进入或离开")
