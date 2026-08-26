"""角色剧场的独立领域模型，绝不复用或修改 StoryState。"""

from __future__ import annotations

from dataclasses import dataclass


SIMULATION_MODES = frozenset({"roleplay", "observer"})
SIMULATION_STATUSES = frozenset({"active", "paused", "completed", "archived"})
TURN_STATUSES = frozenset({"queued", "running", "completed", "failed"})
INPUT_TYPES = frozenset({"speech_action", "director_event", "observer_continue"})


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class SimulationCharacter:
    character_id: str
    name: str
    origin: str  # canonical | sandbox_npc | user_created
    role: str
    goal: str
    secret: str
    public_profile: str
    speech_style: str = ""

    def __post_init__(self) -> None:
        for field in ("character_id", "name", "origin", "role", "goal", "public_profile"):
            _text(getattr(self, field), field)
        if self.origin not in {"canonical", "sandbox_npc", "user_created"}:
            raise ValueError("人物 origin 不合法")


@dataclass(frozen=True, slots=True)
class SimulationCharacterState:
    character_id: str
    emotion: str
    current_intent: str
    attitude: str
    known_fact_ids: tuple[str, ...] = ()
    concealed_fact_ids: tuple[str, ...] = ()
    possessions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SimulationEvent:
    event_id: str
    description: str
    status: str = "active"
    source_turn: int = 0
    observable_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SimulationState:
    current_time: str
    current_location: str
    scene_summary: str
    active_event: str | None
    scene_status: str
    present_character_ids: tuple[str, ...]
    characters: tuple[SimulationCharacterState, ...]
    relationships: tuple[dict[str, object], ...] = ()
    active_events: tuple[SimulationEvent, ...] = ()
    inventory: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        for field in ("current_time", "current_location", "scene_summary", "scene_status"):
            _text(getattr(self, field), field)
        if not self.present_character_ids:
            raise ValueError("场景至少需要一名在场人物")


@dataclass(frozen=True, slots=True)
class SimulationStateDelta:
    character_changes: tuple[dict[str, object], ...] = ()
    relationship_changes: tuple[dict[str, object], ...] = ()
    knowledge_changes: tuple[dict[str, object], ...] = ()
    event_changes: tuple[dict[str, object], ...] = ()
    character_presence_changes: tuple[dict[str, object], ...] = ()
    active_event: str | None = None
    # 仅由场景导演提出，Reducer 负责限制并应用，角色 Agent 无权直接修改舞台。
    scene_update: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SimulationSession:
    simulation_id: str
    book_id: str
    base_book_version: int
    base_chapter_number: int
    mode: str
    user_character_id: str | None
    status: str
    current_turn: int
    version: int
    base_snapshot: dict[str, object]
    scene_config: dict[str, object]
    current_state: SimulationState
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.mode not in SIMULATION_MODES:
            raise ValueError("模拟模式不合法")
        if self.status not in SIMULATION_STATUSES:
            raise ValueError("模拟状态不合法")
        if self.mode == "roleplay" and not self.user_character_id:
            raise ValueError("角色扮演模式必须指定用户人物")


@dataclass(frozen=True, slots=True)
class SimulationTurn:
    turn_id: str
    simulation_id: str
    turn_number: int
    job_id: str | None
    client_request_id: str
    mode: str
    user_character_id: str | None
    target_character_id: str | None
    input_type: str
    user_input: str
    status: str
    output: dict[str, object] | None
    state_delta: dict[str, object] | None
    state_before_hash: str | None
    state_after_hash: str | None
    context_snapshot_id: str | None
    created_at: str
