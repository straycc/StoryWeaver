from __future__ import annotations

import pytest

from storyweaver.story_simulation.models import (
    SimulationCharacterState,
    SimulationState,
    SimulationStateDelta,
)
from storyweaver.story_simulation.reducer import SimulationStateReducer
from storyweaver.story_simulation.agent import RoleplayTurnOutput
from storyweaver.story_simulation.agent import CharacterContributionOutput, RoleplayDeltaOutput, TurnPlanOutput
from storyweaver.story_simulation.models import SimulationSession, SimulationTurn
from storyweaver.story_simulation.runtime import RoleplayRuntime, TurnAssembler, TurnRouter
from storyweaver.story_simulation.context_builder import RoleplayContextBuilder


def _state() -> SimulationState:
    return SimulationState(
        current_time="夜", current_location="客栈", scene_summary="试探", active_event=None,
        scene_status="ongoing", present_character_ids=("lin",),
        characters=(SimulationCharacterState("lin", "平静", "试探", "中立"),),
    )


def test_simulation_reducer_rejects_unknown_knowledge() -> None:
    with pytest.raises(ValueError, match="有来源"):
        SimulationStateReducer().apply(
            state=_state(),
            delta=SimulationStateDelta(knowledge_changes=({"character_id": "lin", "fact_id": "missing"},)),
            known_fact_ids=set(), source_turn=1,
        )


def test_simulation_reducer_changes_presence_without_tracking_location() -> None:
    state = SimulationState(
        current_time="夜", current_location="客栈", scene_summary="试探", active_event=None,
        scene_status="ongoing", present_character_ids=("lin",),
        characters=(
            SimulationCharacterState("lin", "平静", "试探", "中立"),
            SimulationCharacterState("gu", "冷静", "观察", "中立"),
        ),
    )
    result = SimulationStateReducer().apply(
        state=state,
        delta=SimulationStateDelta(character_presence_changes=({"character_id": "gu", "action": "enter"},)),
        known_fact_ids=set(), source_turn=1,
    )
    assert result.present_character_ids == ("lin", "gu")


def test_simulation_reducer_allows_auditable_relationship_evolution() -> None:
    state = SimulationState(
        current_time="夜", current_location="客栈", scene_summary="试探", active_event=None,
        scene_status="ongoing", present_character_ids=("lin", "gu"),
        characters=(
            SimulationCharacterState("lin", "平静", "试探", "中立"),
            SimulationCharacterState("gu", "冷静", "观察", "中立"),
        ),
        relationships=({"source_character_id": "lin", "target_character_id": "gu", "relation_type": "trust", "value": "戒备", "reason": "旧事", "source_turn": 1},),
    )
    result = SimulationStateReducer().apply(
        state=state,
        delta=SimulationStateDelta(relationship_changes=({
            "source_character_id": "lin", "target_character_id": "gu",
            "relation_type": "trust", "value": "初步信任", "reason": "对方公开了线索",
        },)),
        known_fact_ids=set(), source_turn=2,
    )
    relationship = result.relationships[0]
    assert relationship["value"] == "初步信任"
    assert relationship["reason"] == "对方公开了线索"
    assert relationship["source_turn"] == 2


def test_simulation_reducer_rejects_unexplained_relationship_change() -> None:
    state = SimulationState(
        current_time="夜", current_location="客栈", scene_summary="试探", active_event=None,
        scene_status="ongoing", present_character_ids=("lin", "gu"),
        characters=(
            SimulationCharacterState("lin", "平静", "试探", "中立"),
            SimulationCharacterState("gu", "冷静", "观察", "中立"),
        ),
    )
    with pytest.raises(ValueError, match="说明原因"):
        SimulationStateReducer().apply(
            state=state,
            delta=SimulationStateDelta(relationship_changes=({
                "source_character_id": "lin", "target_character_id": "gu", "relation_type": "trust",
            },)),
            known_fact_ids=set(), source_turn=1,
        )


def test_simulation_reducer_only_allows_explained_scene_transition() -> None:
    state = _state()
    with pytest.raises(ValueError, match="转场原因"):
        SimulationStateReducer().apply(
            state=state,
            delta=SimulationStateDelta(scene_update={"current_location": "后院"}),
            known_fact_ids=set(), source_turn=1,
        )
    result = SimulationStateReducer().apply(
        state=state,
        delta=SimulationStateDelta(scene_update={
            "current_time": "夜深", "current_location": "客栈后院",
            "transition_reason": "众人追随脚步声穿过后门",
            "scene_summary": "三人在后院发现新线索", "active_event": "门外脚步声",
        }),
        known_fact_ids=set(), source_turn=1,
    )
    assert result.current_time == "夜深"
    assert result.current_location == "客栈后院"
    assert result.scene_summary == "三人在后院发现新线索"


def test_roleplay_output_accepts_dialogue_array_alias() -> None:
    output = RoleplayTurnOutput.model_validate({
        "dialogue": [{"type": "narration", "content": "风声骤紧。"}],
        "proposed_delta": {},
    })
    assert output.blocks[0].type == "narration"


def test_director_plan_normalizes_legacy_instruction_and_scene_update_fields() -> None:
    plan = TurnPlanOutput.model_validate({
        "actor_ids": ["boss"],
        "director_instructions": "老板先试探来意。",
        "scene_updates": {"scene_summary": "客栈内的试探升级"},
    })
    assert plan.scene_instruction == "老板先试探来意。"
    assert plan.scene_update == {"scene_summary": "客栈内的试探升级"}


def test_director_plan_normalizes_common_scene_update_aliases() -> None:
    plan = TurnPlanOutput.model_validate({
        "actor_ids": ["boss"],
        "scene_instruction": "老板观察门外。",
        "scene_updates": {"summary": "门外传来脚步", "time": "夜深"},
    })
    assert plan.scene_update == {"scene_summary": "门外传来脚步", "current_time": "夜深"}


def test_director_plan_ignores_legacy_tension_field() -> None:
    plan = TurnPlanOutput.model_validate({
        "actor_ids": ["boss"],
        "scene_instruction": "老板压低声音。",
        "scene_update": {"tension_level": 6, "scene_summary": "压迫感上升"},
    })
    assert plan.scene_update == {"scene_summary": "压迫感上升"}


def test_director_plan_allows_runtime_to_fill_missing_actor_ids() -> None:
    plan = TurnPlanOutput.model_validate({
        "scene_instruction": "门外动静引起在场人物反应。",
        "scene_update": {"scene_summary": "门外脚步声逼近"},
    })
    assert plan.actor_ids == []


def test_director_plan_normalizes_narrative_location_and_time_updates() -> None:
    plan = TurnPlanOutput.model_validate({
        "actor_ids": ["boss"],
        "scene_instruction": "众人循声转入后院。",
        "scene_update": {
            "location_change": {"new_location": "客栈后院", "reason": "众人追随门外脚步声"},
            "time_progression": "夜深",
        },
    })
    assert plan.scene_update == {
        "current_location": "客栈后院",
        "transition_reason": "众人追随门外脚步声",
        "current_time": "夜深",
    }


def test_director_plan_discards_unsupported_scene_fields_instead_of_failing_turn() -> None:
    plan = TurnPlanOutput.model_validate({
        "actor_ids": ["boss"],
        "scene_instruction": "众人暂且按兵不动。",
        "scene_update": {
            "present_character_ids": ["boss", "girl"],
            "camera_note": "镜头转向门外",
            "scene_summary": "众人听见门外脚步声",
        },
    })
    assert plan.scene_update == {"scene_summary": "众人听见门外脚步声"}


def test_character_contribution_normalizes_speech_and_action_blocks() -> None:
    contribution = CharacterContributionOutput.model_validate({
        "blocks": [
            {"type": "action", "actor_id": "boss", "content": "老板攥紧柜台边缘", "emotion": "戒备"},
            {"type": "speech", "speaker": "boss", "content": "这里不欢迎外人。", "emotion": "冷淡"},
            {"type": "narration", "content": "灯火微晃。", "emotion": "不应保留"},
        ],
    })
    assert contribution.blocks[0].type == "dialogue"
    assert contribution.blocks[0].speaker_id == "boss"
    assert contribution.blocks[0].action == "老板攥紧柜台边缘"
    assert contribution.blocks[1].speaker_id == "boss"
    assert contribution.blocks[2].type == "narration"


def test_character_contribution_moves_top_level_block_into_empty_blocks() -> None:
    contribution = CharacterContributionOutput.model_validate({
        "blocks": [],
        "type": "narration",
        "content": "林外传来一阵脚步声。",
    })
    assert len(contribution.blocks) == 1
    assert contribution.blocks[0].type == "narration"
    assert contribution.blocks[0].content == "林外传来一阵脚步声。"


def _simulation(*, mode: str = "roleplay") -> SimulationSession:
    state = SimulationState(
        current_time="夜", current_location="客栈", scene_summary="试探", active_event=None,
        scene_status="ongoing", present_character_ids=("user", "boss", "girl"),
        characters=(
            SimulationCharacterState("user", "平静", "询问", "中立"),
            SimulationCharacterState("boss", "戒备", "隐瞒", "敌意"),
            SimulationCharacterState("girl", "紧张", "旁观", "中立"),
        ),
    )
    snapshot = {"characters": [
        {"character_id": "user", "name": "林默", "origin": "canonical", "role": "客人", "goal": "追问", "secret": "无", "public_profile": "客人"},
        {"character_id": "boss", "name": "老板", "origin": "canonical", "role": "掌柜", "goal": "隐瞒", "secret": "钥匙", "public_profile": "掌柜"},
        {"character_id": "girl", "name": "前台女孩", "origin": "canonical", "role": "店员", "goal": "观察", "secret": "无", "public_profile": "店员"},
    ]}
    return SimulationSession("sim", "book", 1, 1, mode, "user" if mode == "roleplay" else None,
                             "active", 0, 1, snapshot, {}, state, "now", "now")


def _turn(*, target: str | None = None, input_type: str = "speech_action", content: str = "老板，你为什么隐瞒？") -> SimulationTurn:
    return SimulationTurn("turn", "sim", 1, "job", "client", "roleplay", "user", target,
                          input_type, content, "queued", None, None, None, None, None, "now")


def test_turn_router_directs_explicit_target_to_one_character() -> None:
    decision = TurnRouter().route(simulation=_simulation(), turn=_turn(target="boss"))
    assert decision.path == "direct_character"
    assert decision.actor_ids == ("boss",)


def test_turn_router_sends_director_event_to_director() -> None:
    decision = TurnRouter().route(simulation=_simulation(), turn=_turn(input_type="director_event", content="突然停电"))
    assert decision.path == "director"


def test_assembler_rejects_character_speaking_for_another_role() -> None:
    contribution = CharacterContributionOutput.model_validate({
        "blocks": [{"type": "dialogue", "speaker_id": "girl", "content": "我不能说。"}],
        "proposed_delta": {},
    })
    with pytest.raises(ValueError, match="只能让被分配角色发言"):
        TurnAssembler().assemble(simulation=_simulation(), actor_ids=("boss",), contributions=(contribution,), plan=None)


def test_assembler_rejects_other_character_state_change() -> None:
    contribution = CharacterContributionOutput.model_validate({
        "blocks": [{"type": "dialogue", "speaker_id": "boss", "content": "出去。"}],
        "proposed_delta": {"character_changes": [{"character_id": "girl", "emotion": "害怕"}]},
    })
    with pytest.raises(ValueError, match="自身状态"):
        TurnAssembler().assemble(simulation=_simulation(), actor_ids=("boss",), contributions=(contribution,), plan=None)


def test_assembler_rejects_character_changing_scene_state() -> None:
    contribution = CharacterContributionOutput.model_validate({
        "blocks": [{"type": "dialogue", "speaker_id": "boss", "content": "跟我来。"}],
        "proposed_delta": {"event_changes": [{"description": "客栈熄灯"}]},
    })
    with pytest.raises(ValueError, match="不得直接修改场景状态"):
        TurnAssembler().assemble(simulation=_simulation(), actor_ids=("boss",), contributions=(contribution,), plan=None)


def test_director_plan_rejects_user_character_as_actor() -> None:
    with pytest.raises(ValueError, match="不属于用户"):
        RoleplayRuntime._validate_plan(
            simulation=_simulation(),
            plan=TurnPlanOutput(actor_ids=["user"], scene_instruction="让用户先回应"),
        )


def test_character_context_does_not_expose_other_character_private_knowledge() -> None:
    simulation = _simulation()
    state = simulation.current_state
    simulation = SimulationSession(
        simulation.simulation_id, simulation.book_id, simulation.base_book_version, simulation.base_chapter_number,
        simulation.mode, simulation.user_character_id, simulation.status, simulation.current_turn, simulation.version,
        simulation.base_snapshot, simulation.scene_config,
        SimulationState(
            state.current_time, state.current_location, state.scene_summary, state.active_event, state.scene_status,
            state.present_character_ids,
            (
                SimulationCharacterState("user", "平静", "询问", "中立", ("user-secret-fact",)),
                SimulationCharacterState("boss", "戒备", "隐瞒", "敌意", ("boss-secret-fact",)),
                SimulationCharacterState("girl", "紧张", "旁观", "中立", ("girl-secret-fact",)),
            ),
        ), simulation.created_at, simulation.updated_at,
    )
    prompt, _ = RoleplayContextBuilder().build_character(
        simulation=simulation, turns=(), request="老板，你知道什么？", input_type="speech_action",
        character_id="boss", scene_instruction="老板回应", visible_contributions=(),
    )
    assert "boss-secret-fact" in prompt
    assert "user-secret-fact" not in prompt
    assert "girl-secret-fact" not in prompt
