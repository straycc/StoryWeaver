"""角色剧场 V2 的 Director 与共享 CharacterAgent。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..llm import WorkerRetryPolicy, WorkerSettings, run_structured_worker, run_with_retry


DIRECTOR_SYSTEM_PROMPT = """你是非正史角色剧场导演，从候选 NPC 中安排 1～2 名有回应动机的角色及行动顺序，给出简短推进目标。
不写台词、旁白或具体动作，不替用户决定，不新增人物或结束正式故事。
可建议已有人物进出、场景摘要、事件和少量时间推进；地点变更必须有明确转场原因。
按提供的 Schema 交付，不推测未提供的人物秘密。"""

CHARACTER_SYSTEM_PROMPT = """你是非正史角色剧场中被分配的角色，只生成自己的台词、可观察行动和自身状态建议。
依据个人已知信息和公开场景回应，体现动机与说话方式，留出用户回应空间。
不替其他角色或用户行动、发言或决定内心，不凭空知道别人的秘密；自己的秘密只在动机和情境允许时透露。
不创建人物或改变时空，状态建议只反映已发生的变化。
按提供的 Schema 交付；dialogue.speaker_id 使用本角色 ID，纯动作时 content 可为空；narration 只描述可观察场景。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DialogueBlock(_Strict):
    type: Literal["dialogue"]
    speaker_id: str
    content: str
    action: str | None = None
    emotion: str | None = None


class NarrationBlock(_Strict):
    type: Literal["narration"]
    content: str


class RoleplayDeltaOutput(_Strict):
    character_changes: list[dict[str, Any]] = Field(default_factory=list)
    relationship_changes: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_changes: list[dict[str, Any]] = Field(default_factory=list)
    event_changes: list[dict[str, Any]] = Field(default_factory=list)
    character_presence_changes: list[dict[str, Any]] = Field(default_factory=list)
    active_event: str | None = None
    scene_update: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_character_map(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        known = {"character_changes", "relationship_changes", "knowledge_changes", "event_changes", "character_presence_changes", "active_event", "scene_update"}
        result = {key: item for key, item in value.items() if key in known}
        changes = list(result.get("character_changes") or [])
        for key, item in value.items():
            if key not in known and isinstance(item, dict):
                changes.append({"character_id": key, **item})
        result["character_changes"] = changes
        return result


def _sanitize_scene_update(value: dict[str, Any]) -> dict[str, Any]:
    """将自由模型的场景描述收敛为 Reducer 唯一接受的白名单。"""

    normalized: dict[str, Any] = {}
    for field in ("current_time", "scene_summary", "active_event"):
        raw = value.get(field)
        if isinstance(raw, str) and raw.strip():
            normalized[field] = raw.strip()
    status = value.get("scene_status")
    if status in {"ongoing", "paused", "completed"}:
        normalized["scene_status"] = status
    location = value.get("current_location")
    reason = value.get("transition_reason")
    if isinstance(location, str) and location.strip() and isinstance(reason, str) and reason.strip():
        normalized["current_location"] = location.strip()
        normalized["transition_reason"] = reason.strip()
    # 例如 present_character_ids、人物坐标、紧张度、镜头说明均不属于 V1 场景状态。
    return normalized


class TurnPlanOutput(_Strict):
    # 漏掉 actor_ids 时由 Runtime 从当前候选 NPC 确定性补齐，避免场景更新白白失败。
    actor_ids: list[str] = Field(default_factory=list, max_length=2)
    scene_instruction: str = Field(min_length=1, max_length=800)
    character_presence_changes: list[dict[str, Any]] = Field(default_factory=list)
    active_event: str | None = Field(default=None, max_length=500)
    scene_status: Literal["ongoing", "paused", "completed"] = "ongoing"
    scene_update: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_director_aliases(cls, value: Any) -> Any:
        """兼容导演常见的复数/说明字段，随后仍以严格 Schema 校验。"""

        if not isinstance(value, dict):
            return value
        raw = dict(value)
        instructions = raw.pop("director_instructions", None)
        updates = raw.pop("scene_updates", None)
        if not raw.get("scene_instruction") and instructions is not None:
            if isinstance(instructions, str):
                raw["scene_instruction"] = instructions
            elif isinstance(instructions, list):
                raw["scene_instruction"] = "；".join(
                    str(item.get("instruction") or item.get("content") or item)
                    for item in instructions if item
                )
        if raw.get("scene_update") is None and updates is not None:
            if isinstance(updates, dict):
                raw["scene_update"] = updates
            elif isinstance(updates, list):
                raw["scene_update"] = next(
                    (item for item in updates if isinstance(item, dict)), None
                )
        scene_update = dict(raw.get("scene_update") or {})
        # 紧张度不是 V1 场景状态；兼容旧输出但不将其写入沙盒。
        scene_update.pop("tension_level", None)
        raw.pop("tension_level", None)
        # 常见自然语言 Schema 会写 summary/location/time，统一为 Reducer 的字段名。
        aliases = {"summary": "scene_summary", "location": "current_location", "time": "current_time"}
        for source, target in aliases.items():
            if source in scene_update and target not in scene_update:
                scene_update[target] = scene_update.pop(source)
        # 兼容更叙事化的字段：location_change 可同时携带目的地和原因。
        location_change = scene_update.pop("location_change", None)
        if location_change is not None and "current_location" not in scene_update:
            if isinstance(location_change, dict):
                scene_update["current_location"] = (
                    location_change.get("current_location")
                    or location_change.get("new_location")
                    or location_change.get("location")
                    or location_change.get("to")
                    or ""
                )
                if not scene_update.get("transition_reason"):
                    scene_update["transition_reason"] = (
                        location_change.get("transition_reason")
                        or location_change.get("reason")
                        or ""
                    )
            else:
                scene_update["current_location"] = location_change
        time_progression = scene_update.pop("time_progression", None)
        if time_progression is not None and "current_time" not in scene_update:
            if isinstance(time_progression, dict):
                scene_update["current_time"] = (
                    time_progression.get("current_time")
                    or time_progression.get("new_time")
                    or time_progression.get("to")
                    or ""
                )
            else:
                scene_update["current_time"] = time_progression
        for key in ("scene_summary", "current_time", "current_location"):
            if key in raw:
                scene_update[key] = raw.pop(key)
        canonical_scene_update = _sanitize_scene_update(scene_update)
        if canonical_scene_update:
            raw["scene_update"] = canonical_scene_update
        else:
            raw.pop("scene_update", None)
        allowed = {
            "actor_ids", "scene_instruction", "character_presence_changes",
            "active_event", "scene_status", "scene_update",
        }
        return {key: item for key, item in raw.items() if key in allowed}


class CharacterContributionOutput(_Strict):
    blocks: list[DialogueBlock | NarrationBlock] = Field(min_length=1, max_length=3)
    proposed_delta: RoleplayDeltaOutput = Field(default_factory=RoleplayDeltaOutput)
    suggested_actions: list[str] = Field(default_factory=list, max_length=3)
    observable_summary: str = Field(default="", max_length=500)

    @model_validator(mode="before")
    @classmethod
    def normalize_common_block_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        blocks_value = result.get("blocks")
        if isinstance(blocks_value, list):
            blocks: list[Any] = list(blocks_value)
        else:
            blocks = []
        if not blocks:
            for key in ("dialogue", "speech", "action", "narration"):
                items = result.pop(key, [])
                if isinstance(items, list):
                    blocks.extend(items)
                elif isinstance(items, dict):
                    blocks.append(items)
            # 有些模型会把单个块平铺在顶层，同时留下 blocks: []。
            top_level_type = result.pop("type", None)
            top_level_content = result.pop("content", None)
            if top_level_type is not None or top_level_content is not None:
                blocks.append({
                    "type": top_level_type or "narration",
                    "content": top_level_content or "",
                    "speaker_id": result.pop("speaker_id", None),
                    "speaker": result.pop("speaker", None),
                    "actor_id": result.pop("actor_id", None),
                    "character_id": result.pop("character_id", None),
                    "action": result.pop("action", None),
                    "emotion": result.pop("emotion", None),
                })
        # 这些字段只允许留在 block 内，避免顶层 extra 字段破坏 DTO。
        for key in ("type", "content", "speaker_id", "speaker", "actor_id", "character_id", "action", "emotion"):
            result.pop(key, None)
        result["blocks"] = blocks
        normalized: list[Any] = []
        for block in result.get("blocks", []):
            if not isinstance(block, dict):
                normalized.append(block)
                continue
            item = dict(block)
            block_type = str(item.get("type") or "").strip().casefold()
            if block_type in {"narrator", "narration", "description"}:
                content = item.get("content") or item.get("text") or item.get("description") or ""
                normalized.append({"type": "narration", "content": str(content)})
                continue
            # speech 与 action 都属于某个角色的可见贡献；action 作为 action 字段展示。
            if block_type in {"speech", "dialogue", "action"}:
                speaker = item.get("speaker_id") or item.get("speaker") or item.get("actor_id") or item.get("character_id")
                content = item.get("content") or item.get("text") or item.get("message") or ""
                output: dict[str, Any] = {
                    "type": "dialogue",
                    "speaker_id": str(speaker or ""),
                    "content": str(content) if block_type != "action" else "",
                }
                if block_type == "action":
                    output["action"] = str(content)
                elif item.get("action"):
                    output["action"] = str(item["action"])
                if item.get("emotion"):
                    output["emotion"] = str(item["emotion"])
                normalized.append(output)
                continue
            normalized.append(item)
        result["blocks"] = normalized
        return result


class RoleplayTurnOutput(_Strict):
    """最终已组装回合的兼容 DTO。"""

    blocks: list[DialogueBlock | NarrationBlock]
    proposed_delta: RoleplayDeltaOutput
    suggested_actions: list[str] = Field(default_factory=list)
    scene_status: Literal["ongoing", "paused", "completed"] = "ongoing"
    turn_summary: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_dialogue_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "blocks" not in value and isinstance(value.get("dialogue"), list):
            value = dict(value)
            value["blocks"] = value.pop("dialogue")
        return value


class SceneDirectorAgent:
    def __init__(self, settings: WorkerSettings, *, event_sinks: tuple[Any, ...] = ()) -> None:
        self._settings = settings
        self._event_sinks = event_sinks

    async def run(self, prompt: str) -> TurnPlanOutput:
        return await _run(settings=self._settings, prompt=prompt, output_type=TurnPlanOutput, event_sinks=self._event_sinks)


class CharacterAgent:
    """共享模板；角色身份仅由每次 prompt 中冻结的 Context 决定。"""

    def __init__(self, settings: WorkerSettings, *, event_sinks: tuple[Any, ...] = ()) -> None:
        self._settings = settings
        self._event_sinks = event_sinks

    async def run(self, prompt: str) -> CharacterContributionOutput:
        return await _run(settings=self._settings, prompt=prompt, output_type=CharacterContributionOutput, event_sinks=self._event_sinks)


async def _run(*, settings: WorkerSettings, prompt: str, output_type: type[BaseModel], event_sinks: tuple[Any, ...]) -> Any:
    async def operation(context: Any) -> Any:
        actual = prompt
        if context.is_repair:
            actual += f"\n\n上次输出校验失败：{context.repair_error}\n只修复并提交合法 JSON。"
        return await run_structured_worker(
            settings=settings, prompt=actual, output_type=output_type,
            event_sinks=event_sinks, tracing_enabled=True,
        )
    return await run_with_retry(
        worker_name=settings.worker_id, operation=operation,
        policy=WorkerRetryPolicy(max_attempts=2, max_repairs=1),
    )
