"""从版本化 YAML 加载小说 A/B 评测 Case。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..novel_creation.models import CreateNovelRequest
from .models import (
    EvaluationCanonFact,
    EvaluationCase,
    EvaluationChapterInput,
    EvaluationChapterSpec,
    EvaluationCharacter,
    EvaluationExpectations,
    EvaluationExperiment,
    EvaluationStateUpdate,
)


SUPPORTED_SCHEMA_VERSION = "novel-ab-v1"


def load_evaluation_case(path: str | Path) -> EvaluationCase:
    """读取并完整校验一个 YAML Case，生成阶段不再重新解释源文件。"""

    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"评测文件必须使用 UTF-8 编码：{source}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取评测文件 {source}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"评测 YAML 无法解析：{exc}") from exc
    root = _mapping(raw, "root")
    version = _text(root, "version", "root")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的评测版本 {version!r}；当前仅支持 {SUPPORTED_SCHEMA_VERSION}"
        )

    experiment_data = _mapping(root.get("experiment"), "experiment")
    book = _mapping(root.get("book"), "book")
    chapters_data = _list(root.get("chapters"), "chapters")
    target_chapters = _positive_int(book, "target_chapters", "book")
    target_words = _positive_int(book, "chapter_target_words", "book")

    request = CreateNovelRequest(
        title=_text(book, "title", "book"),
        genre=_text(book, "genre", "book"),
        premise=_text(book, "premise", "book"),
        protagonist=_text(book, "protagonist", "book"),
        central_conflict=_text(book, "central_conflict", "book"),
        tone=_text(book, "tone", "book"),
        target_chapters=target_chapters,
        chapter_target_words=target_words,
        language=str(book.get("language", "zh")).strip() or "zh",
    )
    experiment = EvaluationExperiment(
        title=_text(experiment_data, "title", "experiment"),
        temperature=_number(experiment_data.get("temperature", 0.6), "experiment.temperature"),
        repetitions=_positive_int_value(
            experiment_data.get("repetitions", 1), "experiment.repetitions"
        ),
        skill_ids=_text_tuple(experiment_data.get("skill_ids", []), "experiment.skill_ids"),
    )
    characters = tuple(
        EvaluationCharacter(
            character_id=_text(item, "id", f"book.characters[{index}]"),
            name=_text(item, "name", f"book.characters[{index}]"),
            description=_text(item, "description", f"book.characters[{index}]"),
        )
        for index, value in enumerate(_list(book.get("characters", []), "book.characters"))
        for item in (_mapping(value, f"book.characters[{index}]"),)
    )
    initial_canon = tuple(
        EvaluationCanonFact(
            fact_id=_text(item, "id", f"book.initial_canon[{index}]"),
            content=_text(item, "content", f"book.initial_canon[{index}]"),
        )
        for index, value in enumerate(_list(book.get("initial_canon", []), "book.initial_canon"))
        for item in (_mapping(value, f"book.initial_canon[{index}]"),)
    )
    chapters = tuple(
        _chapter(value, index=index) for index, value in enumerate(chapters_data)
    )
    if len(chapters) != target_chapters:
        raise ValueError(
            f"chapters 实际包含 {len(chapters)} 章，与 book.target_chapters={target_chapters} 不一致"
        )
    return EvaluationCase(
        case_id=_text(root, "case_id", "root"),
        description=_text(root, "description", "root"),
        request=request,
        experiment=experiment,
        characters=characters,
        initial_canon=initial_canon,
        chapters=chapters,
    )


def _chapter(value: object, *, index: int) -> EvaluationChapterSpec:
    path = f"chapters[{index}]"
    data = _mapping(value, path)
    input_data = _mapping(data.get("input"), f"{path}.input")
    expectations_data = _mapping(
        data.get("expectations", {}), f"{path}.expectations"
    )
    updates = tuple(
        EvaluationStateUpdate(
            subject=_text(item, "subject", f"{path}.expectations.state_updates[{update_index}]"),
            field=_text(item, "field", f"{path}.expectations.state_updates[{update_index}]"),
            old_value=_text(item, "old_value", f"{path}.expectations.state_updates[{update_index}]"),
            new_value=_text(item, "new_value", f"{path}.expectations.state_updates[{update_index}]"),
        )
        for update_index, update in enumerate(
            _list(expectations_data.get("state_updates", []), f"{path}.expectations.state_updates")
        )
        for item in (_mapping(update, f"{path}.expectations.state_updates[{update_index}]"),)
    )
    return EvaluationChapterSpec(
        number=_positive_int(data, "number", path),
        title=_text(data, "title", path),
        input=EvaluationChapterInput(
            objective=_text(input_data, "objective", f"{path}.input"),
            required_beats=_text_tuple(
                input_data.get("required_beats"), f"{path}.input.required_beats"
            ),
            constraints=_text_tuple(
                input_data.get("constraints", []), f"{path}.input.constraints"
            ),
        ),
        expectations=EvaluationExpectations(
            required_facts=_text_tuple(
                expectations_data.get("required_facts", []),
                f"{path}.expectations.required_facts",
            ),
            forbidden_events=_text_tuple(
                expectations_data.get("forbidden_events", []),
                f"{path}.expectations.forbidden_events",
            ),
            continuity_sources=_text_tuple(
                expectations_data.get("continuity_sources", []),
                f"{path}.expectations.continuity_sources",
            ),
            focus=_text_tuple(
                expectations_data.get("focus", []), f"{path}.expectations.focus"
            ),
            state_updates=updates,
        ),
    )


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是对象")
    return {str(key): item for key, item in value.items()}


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{path} 必须是数组")
    return value


def _text(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key} 必须是非空字符串")
    return value.strip()


def _text_tuple(value: object, path: str) -> tuple[str, ...]:
    values = _list(value, path)
    result: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path}[{index}] 必须是非空字符串")
        result.append(item.strip())
    return tuple(result)


def _positive_int(data: dict[str, Any], key: str, path: str) -> int:
    return _positive_int_value(data.get(key), f"{path}.{key}")


def _positive_int_value(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{path} 必须是正整数")
    return value


def _number(value: object, path: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{path} 必须是数字")
    return float(value)
