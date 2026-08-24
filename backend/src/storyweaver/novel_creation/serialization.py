"""小说领域模型的严格 JSON 序列化与反序列化。"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .exceptions import SerializationError
from .models import (
    BookMetadata,
    ChapterDraft,
    ChapterCandidateMetadata,
    ChapterMetadata,
    ChapterPlan,
    ChapterPlanProposal,
    ChapterRewriteRecord,
    ChapterResult,
    ChapterSummary,
    CharacterProfile,
    CharacterState,
    CharacterStateUpdate,
    ChapterContext,
    ContextEntry,
    ContextTrace,
    FactRecord,
    HookPlan,
    HookUpdate,
    NovelFoundation,
    OutlineNode,
    ReviewIssue,
    ReviewReport,
    StoryHook,
    StoryState,
    StoryStateDelta,
    persisted_field_names,
)


T = TypeVar("T")
Decoder = Callable[[object], T]


def to_data(value: object) -> object:
    """把已注册领域模型递归转换为 JSON 兼容数据。"""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [to_data(item) for item in value]
    if isinstance(value, list):
        return [to_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_data(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SerializationError(f"不支持序列化的数据类型：{type(value).__name__}")


def dumps_json(value: object, *, pretty: bool = True) -> str:
    """生成 UTF-8 友好的 JSON 文本。"""

    return json.dumps(
        to_data(value),
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=True,
    ) + ("\n" if pretty else "")


def loads_json(text: str) -> object:
    """解析 JSON，并统一转换底层解析错误。"""

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SerializationError(f"JSON 格式错误：{exc.msg}") from exc


def load_json_file(path: Path) -> object:
    try:
        return loads_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SerializationError(f"无法读取文件 {path}：{exc}") from exc


def decode_book_metadata(data: object) -> BookMetadata:
    return _construct(BookMetadata, data)


def decode_character_profile(data: object) -> CharacterProfile:
    return _construct(
        CharacterProfile,
        data,
        tuple_fields=("personality", "knowledge_boundaries"),
    )


def decode_outline_node(data: object) -> OutlineNode:
    return _construct(OutlineNode, data, tuple_fields=("expected_changes",))


def decode_story_hook(data: object) -> StoryHook:
    return _construct(StoryHook, data)


def decode_novel_foundation(data: object) -> NovelFoundation:
    return _construct(
        NovelFoundation,
        data,
        tuple_fields=("writing_rules",),
        nested_tuple_fields={
            "characters": decode_character_profile,
            "outline": decode_outline_node,
            "initial_hooks": decode_story_hook,
        },
    )


def decode_chapter_plan(data: object) -> ChapterPlan:
    return _construct(
        ChapterPlan,
        data,
        tuple_fields=(
            "participating_character_ids",
            "required_beats",
            "forbidden_events",
            "relevant_hook_ids",
            "style_focus",
        ),
        nested_fields={"hook_plan": decode_hook_plan},
    )


def decode_hook_plan(data: object) -> HookPlan:
    return _construct(
        HookPlan,
        data,
        tuple_fields=("advance_hook_ids", "resolve_hook_ids"),
    )


def decode_chapter_plan_proposal(data: object) -> ChapterPlanProposal:
    return _construct(
        ChapterPlanProposal,
        data,
        tuple_fields=(
            "feedback_history",
            "selected_memory_ids",
            "selected_memory_descriptions",
        ),
        nested_fields={"plan": decode_chapter_plan},
    )


def decode_chapter_draft(data: object) -> ChapterDraft:
    return _construct(ChapterDraft, data)


def decode_review_issue(data: object) -> ReviewIssue:
    return _construct(ReviewIssue, data, tuple_fields=("related_source_ids",))


def decode_review_report(data: object) -> ReviewReport:
    return _construct(
        ReviewReport,
        data,
        nested_tuple_fields={"issues": decode_review_issue},
    )


def decode_chapter_summary(data: object) -> ChapterSummary:
    return _construct(ChapterSummary, data)


def decode_context_entry(data: object) -> ContextEntry:
    return _construct(ContextEntry, data)


def decode_chapter_context(data: object) -> ChapterContext:
    return _construct(
        ChapterContext,
        data,
        nested_tuple_fields={"entries": decode_context_entry},
    )


def decode_context_trace(data: object) -> ContextTrace:
    return _construct(
        ContextTrace,
        data,
        tuple_fields=(
            "selected_source_ids",
            "excluded_source_ids",
            "protected_source_ids",
            "notes",
        ),
    )


def decode_character_state(data: object) -> CharacterState:
    return _construct(
        CharacterState,
        data,
        tuple_fields=("possessions", "known_fact_ids"),
    )


def decode_fact_record(data: object) -> FactRecord:
    return _construct(FactRecord, data)


def decode_story_state(data: object) -> StoryState:
    return _construct(
        StoryState,
        data,
        nested_tuple_fields={
            "characters": decode_character_state,
            "facts": decode_fact_record,
            "hooks": decode_story_hook,
        },
    )


def decode_character_state_update(data: object) -> CharacterStateUpdate:
    return _construct(
        CharacterStateUpdate,
        data,
        tuple_fields=(
            "add_possessions",
            "remove_possessions",
            "learned_fact_ids",
        ),
    )


def decode_hook_update(data: object) -> HookUpdate:
    return _construct(HookUpdate, data)


def decode_story_state_delta(data: object) -> StoryStateDelta:
    return _construct(
        StoryStateDelta,
        data,
        tuple_fields=("invalidated_fact_ids",),
        nested_tuple_fields={
            "character_updates": decode_character_state_update,
            "new_facts": decode_fact_record,
            "new_hooks": decode_story_hook,
            "hook_updates": decode_hook_update,
        },
    )


def decode_chapter_metadata(data: object) -> ChapterMetadata:
    return _construct(ChapterMetadata, data)


def decode_chapter_rewrite_record(data: object) -> ChapterRewriteRecord:
    return _construct(
        ChapterRewriteRecord,
        data,
        tuple_fields=("archived_chapter_numbers",),
    )


def decode_chapter_candidate_metadata(data: object) -> ChapterCandidateMetadata:
    return _construct(ChapterCandidateMetadata, data)


def decode_chapter_result(data: object) -> ChapterResult:
    return _construct(
        ChapterResult,
        data,
        nested_fields={
            "plan": decode_chapter_plan,
            "draft": decode_chapter_draft,
            "final_draft": decode_chapter_draft,
            "initial_review": decode_review_report,
            "final_review": decode_review_report,
            "state_delta": lambda value: (
                None if value is None else decode_story_state_delta(value)
            ),
            "context_trace": decode_context_trace,
        },
        nested_tuple_fields={
            "draft_history": decode_chapter_draft,
            "review_history": decode_review_report,
        },
    )


def decode_chapter_index(data: object) -> tuple[ChapterMetadata, ...]:
    return tuple(
        decode_chapter_metadata(item)
        for item in _require_array(data, "chapter_index")
    )


def _construct(
    model_type: type[T],
    data: object,
    *,
    tuple_fields: tuple[str, ...] = (),
    nested_fields: Mapping[str, Decoder[Any]] | None = None,
    nested_tuple_fields: Mapping[str, Decoder[Any]] | None = None,
) -> T:
    mapping = _require_object(data, model_type.__name__)
    allowed_fields = persisted_field_names(model_type)
    unknown_fields = set(mapping) - allowed_fields
    if unknown_fields:
        names = ", ".join(sorted(str(name) for name in unknown_fields))
        raise SerializationError(f"{model_type.__name__} 包含未知字段：{names}")

    model_fields = fields(model_type)
    required_fields = {
        field.name
        for field in model_fields
        if field.default is MISSING and field.default_factory is MISSING
    }
    missing_fields = required_fields - set(mapping)
    if missing_fields:
        names = ", ".join(sorted(missing_fields))
        raise SerializationError(f"{model_type.__name__} 缺少字段：{names}")

    values = dict(mapping)
    for field_name in tuple_fields:
        if field_name in values:
            values[field_name] = tuple(
                _require_array(values[field_name], f"{model_type.__name__}.{field_name}")
            )
    for field_name, decoder in (nested_fields or {}).items():
        if field_name in values:
            values[field_name] = decoder(values[field_name])
    for field_name, decoder in (nested_tuple_fields or {}).items():
        if field_name in values:
            values[field_name] = tuple(
                decoder(item)
                for item in _require_array(
                    values[field_name],
                    f"{model_type.__name__}.{field_name}",
                )
            )

    try:
        return model_type(**values)
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"{model_type.__name__} 数据无效：{exc}") from exc


def _require_object(data: object, label: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise SerializationError(f"{label} 必须是 JSON 对象")
    if any(not isinstance(key, str) for key in data):
        raise SerializationError(f"{label} 的字段名必须是字符串")
    return data


def _require_array(data: object, label: str) -> list[object]:
    if not isinstance(data, list):
        raise SerializationError(f"{label} 必须是 JSON 数组")
    return data
