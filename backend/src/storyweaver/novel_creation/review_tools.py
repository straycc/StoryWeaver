"""Reviewer 受限自治使用的只读正史证据工具。"""

from __future__ import annotations

import re
from dataclasses import dataclass
import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Mapping

from agents.tool import FunctionTool
from pydantic import BaseModel, ConfigDict, Field

from .models import ChapterSummary, NovelProject


_QUERY_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ReadToolDescription:
    """只读工具的本地说明；SDK FunctionTool 由工厂直接构造。"""

    name: str
    description: str
    input_schema: dict[str, Any]


class _ToolInput(BaseModel):
    """SDK 工具参数的本地严格校验基类。"""

    model_config = ConfigDict(extra="forbid")


class EntityEvidenceInput(_ToolInput):
    entity_type: Literal["character"] = "character"
    entity_id: str = Field(min_length=1)


class CanonEvidenceSearchInput(_ToolInput):
    query: str = Field(min_length=1, max_length=80)
    foreshadowing_only: bool = False
    limit: int = Field(default=5, ge=1, le=5)


class ChapterSummaryInput(_ToolInput):
    # 章节号无效时由工具返回可读的“未命中”结果，而不是让一次误检索
    # 中断模型的证据链。真正的章节读取始终只允许正整数。
    chapter_number: int = 0


class FoundationQueryInput(_ToolInput):
    section: Literal["auto", "characters", "world", "writing_rules"] = "auto"
    query: str = Field(default="", max_length=80)
    limit: int = Field(default=3, ge=1, le=3)


class OpenForeshadowingsInput(_ToolInput):
    limit: int = Field(default=8, ge=1, le=10)


TOOL_INPUT_MODELS: dict[str, type[_ToolInput]] = {
    "get_entity_evidence": EntityEvidenceInput,
    "search_canon_evidence": CanonEvidenceSearchInput,
    "read_chapter_summary": ChapterSummaryInput,
    "query_foundation": FoundationQueryInput,
    "list_open_foreshadowings": OpenForeshadowingsInput,
}

_TOOL_DESCRIPTIONS = {
    "get_entity_evidence": "查询角色当前状态、持有物和相关权威事实，仅用于核验具体疑点。",
    "search_canon_evidence": "按关键词查询权威事实和伏笔，未找到不代表正文错误。",
    "read_chapter_summary": "读取已提交章节的压缩摘要，不返回全文。",
    "query_foundation": "查询人物稳定设定、世界规则或写作硬约束，不返回动态状态。",
    "list_open_foreshadowings": "列出尚未解决的伏笔，用于核验推进或回收状态。",
}


def _input_schema(model: type[_ToolInput]) -> dict[str, Any]:
    """从 Pydantic DTO 派生工具 Schema，避免运行时出现第二份参数契约。"""

    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _clip(value: str, *, maximum: int = 800) -> str:
    """限制单个证据字段长度，避免只读工具结果挤占审查上下文。"""

    if len(value) <= maximum:
        return value
    return f"{value[:maximum]}…"


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """一次审查期间不可变的权威资料快照。"""

    project: NovelProject
    chapter_summaries: tuple[ChapterSummary, ...]

    @property
    def book_id(self) -> str:
        return self.project.metadata.book_id


class EntityEvidenceTool:
    """查询角色当前状态及其相关正史事实。"""

    _DEFINITION = ReadToolDescription(
        name="get_entity_evidence",
        description=(
            "查询角色当前的权威状态、持有物、已知事实及相关正史证据。"
            "仅用于核验正文中已经出现的具体疑点。"
        ),
        input_schema=_input_schema(EntityEvidenceInput),
    )

    def __init__(self, snapshot: ReviewSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def definition(self) -> ReadToolDescription:
        return self._DEFINITION

    async def execute(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        entity_type = arguments.get("entity_type")
        entity_id = arguments.get("entity_id")
        if entity_type != "character":
            raise ValueError("get_entity_evidence 的 entity_type 仅支持 character")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise ValueError("entity_id 必须是非空字符串")

        character_id = entity_id.strip()
        character = next(
            (
                item
                for item in self._snapshot.project.state.characters
                if item.character_id == character_id
            ),
            None,
        )
        profile = next(
            (
                item
                for item in self._snapshot.project.foundation.characters
                if item.character_id == character_id
            ),
            None,
        )
        if character is None or profile is None:
            return {"matched": False, "entity_id": character_id}

        current_facts = tuple(
            fact
            for fact in self._snapshot.project.state.current_facts
            if fact.subject_id == character_id or fact.fact_id in character.known_fact_ids
        )[:5]
        historical_facts = tuple(
            fact
            for fact in self._snapshot.project.state.facts
            if not fact.is_current
            and (fact.subject_id == character_id or fact.fact_id in character.known_fact_ids)
        )[-3:]
        return {
            "matched": True,
            "entity": {
                "entity_id": character_id,
                "entity_type": "character",
                "display_name": profile.name,
            },
            "current_state": {
                "location": character.location,
                "status": character.status,
                "current_goal": character.current_goal,
                "emotion": character.emotion,
                "possessions": character.possessions,
            },
            "current_facts": [self._fact_data(fact) for fact in current_facts],
            "superseded_facts": [self._fact_data(fact) for fact in historical_facts],
        }

    @staticmethod
    def _fact_data(fact) -> dict[str, object]:
        return {
            "fact_id": fact.fact_id,
            "subject_id": fact.subject_id,
            "predicate": fact.predicate,
            "value": fact.value,
            "source_chapter": fact.source_chapter,
            "valid_from_chapter": fact.valid_from_chapter,
            "valid_until_chapter": fact.valid_until_chapter,
            "importance": fact.importance,
        }


class CanonEvidenceSearchTool:
    """按确定性关键词查找事实和伏笔，不调用模型或修改正史。"""

    _DEFINITION = ReadToolDescription(
        name="search_canon_evidence",
        description=(
            "按关键词查找权威事实和伏笔，返回来源章节、有效状态和原始 canon。"
            "未找到结果不代表正文错误。"
        ),
        input_schema=_input_schema(CanonEvidenceSearchInput),
    )

    def __init__(self, snapshot: ReviewSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def definition(self) -> ReadToolDescription:
        return self._DEFINITION

    async def execute(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 80:
            raise ValueError("query 必须是长度为 1 到 80 的非空字符串")
        foreshadowing_only = arguments.get("foreshadowing_only", False)
        if not isinstance(foreshadowing_only, bool):
            raise ValueError("foreshadowing_only 必须是 bool")
        limit = arguments.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
            raise ValueError("limit 必须是 1 到 5 的整数")

        normalized = self._normalize(query)
        facts = ()
        if not foreshadowing_only:
            facts = tuple(
                fact
                for fact in self._snapshot.project.state.facts
                if self._matches(
                    normalized,
                    f"{fact.fact_id} {fact.subject_id} {fact.predicate} {fact.value}",
                )
            )[:limit]
        hooks = tuple(
            hook
            for hook in self._snapshot.project.state.hooks
            if self._matches(
                normalized,
                f"{hook.hook_id} {hook.description}",
            )
        )[:limit]
        return {
            "query": query.strip(),
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "subject_id": fact.subject_id,
                    "predicate": fact.predicate,
                    "value": fact.value,
                    "status": "current" if fact.is_current else "superseded",
                    "source_chapter": fact.source_chapter,
                    "valid_from_chapter": fact.valid_from_chapter,
                    "valid_until_chapter": fact.valid_until_chapter,
                    "importance": fact.importance,
                }
                for fact in facts
            ],
            "hooks": [
                {
                    "hook_id": hook.hook_id,
                    "description": hook.description,
                    "status": hook.status,
                    "opened_chapter": hook.opened_chapter,
                    "last_advanced_chapter": hook.last_advanced_chapter,
                    "importance": hook.importance,
                }
                for hook in hooks
            ],
            "truncated": False,
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return _QUERY_SPACE.sub("", value.casefold())

    @classmethod
    def _matches(cls, query: str, value: str) -> bool:
        return query in cls._normalize(value)


class ChapterSummaryTool:
    """读取已提交章节的压缩摘要，用于核验事件顺序。"""

    _DEFINITION = ReadToolDescription(
        name="read_chapter_summary",
        description=(
            "读取指定已提交章节的权威压缩摘要，用于核验事件顺序、"
            "关键转折和前后承接；不返回整章正文。"
        ),
        input_schema=_input_schema(ChapterSummaryInput),
    )

    def __init__(self, snapshot: ReviewSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def definition(self) -> ReadToolDescription:
        return self._DEFINITION

    async def execute(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        chapter_number = arguments.get("chapter_number")
        if (
            not isinstance(chapter_number, int)
            or isinstance(chapter_number, bool)
            or chapter_number <= 0
        ):
            return {
                "matched": False,
                "chapter_number": chapter_number,
                "reason": "chapter_number 必须是大于 0 的整数",
            }
        summary = next(
            (
                item
                for item in self._snapshot.chapter_summaries
                if item.chapter_number == chapter_number
            ),
            None,
        )
        if summary is None:
            return {
                "matched": False,
                "chapter_number": chapter_number,
                "reason": "该章节不在当前审查快照中",
            }
        return {
            "matched": True,
            "chapter_number": summary.chapter_number,
            "summary": _clip(summary.summary, maximum=1200),
        }


class FoundationQueryTool:
    """按章节审查需要查询稳定人物设定、世界规则和写作硬约束。"""

    _DEFINITION = ReadToolDescription(
        name="query_foundation",
        description=(
            "查询小说基础设定中的人物稳定属性、世界规则或写作硬约束。"
            "只返回与查询主题匹配的基础设定，不返回当前动态状态。"
        ),
        input_schema=_input_schema(FoundationQueryInput),
    )

    def __init__(self, snapshot: ReviewSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def definition(self) -> ReadToolDescription:
        return self._DEFINITION

    async def execute(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        section = arguments.get("section", "auto")
        if section not in {"auto", "characters", "world", "writing_rules"}:
            raise ValueError("section 不支持")
        query = arguments.get("query", "")
        if not isinstance(query, str) or len(query.strip()) > 80:
            raise ValueError("query 必须是最长 80 字的字符串")
        limit = arguments.get("limit", 3)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 3:
            raise ValueError("limit 必须是 1 到 3 的整数")

        normalized_query = CanonEvidenceSearchTool._normalize(query)
        foundation = self._snapshot.project.foundation
        characters = ()
        if section in {"auto", "characters"}:
            characters = tuple(
                profile
                for profile in foundation.characters
                if not normalized_query
                or CanonEvidenceSearchTool._matches(
                    normalized_query,
                    " ".join(
                        (
                            profile.character_id,
                            profile.name,
                            profile.role,
                            profile.motivation,
                            profile.long_term_goal,
                            profile.conflict,
                            profile.speech_style,
                            *profile.personality,
                            *profile.knowledge_boundaries,
                        )
                    ),
                )
            )[:limit]
        world_text = "\n".join(
            (
                foundation.premise,
                foundation.world_setting,
                foundation.central_conflict,
                foundation.ending_direction,
            )
        )
        include_world = section in {"auto", "world"} and (
            not normalized_query
            or CanonEvidenceSearchTool._matches(normalized_query, world_text)
        )
        rules = ()
        if section in {"auto", "writing_rules"}:
            rules = tuple(
                rule
                for rule in foundation.writing_rules
                if not normalized_query
                or CanonEvidenceSearchTool._matches(normalized_query, rule)
            )[:limit]
        return {
            "section": section,
            "query": query.strip(),
            "characters": [
                {
                    "character_id": profile.character_id,
                    "name": profile.name,
                    "role": profile.role,
                    "personality": profile.personality,
                    "motivation": _clip(profile.motivation),
                    "long_term_goal": _clip(profile.long_term_goal),
                    "conflict": _clip(profile.conflict),
                    "speech_style": _clip(profile.speech_style),
                    "knowledge_boundaries": profile.knowledge_boundaries,
                }
                for profile in characters
            ],
            "world": (
                {
                    "premise": _clip(foundation.premise),
                    "world_setting": _clip(foundation.world_setting),
                    "central_conflict": _clip(foundation.central_conflict),
                    "ending_direction": _clip(foundation.ending_direction),
                }
                if include_world
                else None
            ),
            "writing_rules": [_clip(rule, maximum=400) for rule in rules],
        }


class OpenForeshadowingsTool:
    """列出当前仍未解决的伏笔，供审查员核验结案与推进状态。"""

    _DEFINITION = ReadToolDescription(
        name="list_open_foreshadowings",
        description=(
            "列出当前仍未解决或仍在推进的伏笔，按重要度和最近推进章节排序。"
            "用于核验正文是否无依据地宣布伏笔已经结案。"
        ),
        input_schema=_input_schema(OpenForeshadowingsInput),
    )

    def __init__(self, snapshot: ReviewSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def definition(self) -> ReadToolDescription:
        return self._DEFINITION

    async def execute(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit 必须是 1 到 10 的整数")
        unresolved = sorted(
            (
                item
                for item in self._snapshot.project.state.hooks
                if item.status != "resolved"
            ),
            key=lambda item: (-item.importance, -item.last_advanced_chapter, item.hook_id),
        )
        selected = unresolved[:limit]
        return {
            "hooks": [
                {
                    "hook_id": item.hook_id,
                    "name": item.display_name,
                    "description": _clip(item.description),
                    "status": item.status,
                    "importance": item.importance,
                    "opened_chapter": item.opened_chapter,
                    "last_advanced_chapter": item.last_advanced_chapter,
                    "expected_payoff": _clip(item.expected_payoff),
                }
                for item in selected
            ],
            "truncated": len(unresolved) > len(selected),
            "total_open_hooks": len(unresolved),
        }


def build_sdk_read_tools(
    *,
    snapshot: ReviewSnapshot,
    max_tool_calls: int,
    on_completed: Callable[[str, int, bool, bool, float, str | None, bool], Awaitable[None]] | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> list[FunctionTool]:
    """从一次性正史快照构造 SDK 只读工具。

    预算在工具开始前以锁原子预占；同一回合并发调用也不能越过上限。
    ``on_completed`` 只用于业务日志投影，不参与工具结果或权限决策。
    """

    if max_tool_calls < 1:
        raise ValueError("max_tool_calls 必须大于 0")
    implementations: dict[str, object] = {
        "get_entity_evidence": EntityEvidenceTool(snapshot),
        "search_canon_evidence": CanonEvidenceSearchTool(snapshot),
        "read_chapter_summary": ChapterSummaryTool(snapshot),
        "query_foundation": FoundationQueryTool(snapshot),
        "list_open_foreshadowings": OpenForeshadowingsTool(snapshot),
    }
    lock = asyncio.Lock()
    counter = 0
    completed_results: dict[str, str] = {}
    in_flight: dict[str, asyncio.Future[str]] = {}

    def build(name: str, implementation: object) -> FunctionTool:
        async def invoke(_context: Any, arguments: str) -> str:
            nonlocal counter
            started_at = time.perf_counter()
            cache_key: str | None = None
            try:
                raw_arguments = json.loads(arguments)
                cache_key = f"{name}:{json.dumps(raw_arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            except Exception:
                # 无法解析的参数仍按真实调用处理，并由下方统一返回错误摘要。
                raw_arguments = None
            duplicate_result: str | None = None
            duplicate_future: asyncio.Future[str] | None = None
            own_future: asyncio.Future[str] | None = None
            async with lock:
                if cache_key is not None and cache_key in completed_results:
                    duplicate_result = completed_results[cache_key]
                    call_index = counter
                elif cache_key is not None and cache_key in in_flight:
                    duplicate_future = in_flight[cache_key]
                    call_index = counter
                else:
                    if counter >= max_tool_calls:
                        if on_completed is not None:
                            await on_completed(name, counter, True, True, 0.0, None, False)
                        return json.dumps(
                            {"error": "工具调用预算已耗尽"}, ensure_ascii=False
                        )
                    counter += 1
                    call_index = counter
                    if cache_key is not None:
                        own_future = asyncio.get_running_loop().create_future()
                        in_flight[cache_key] = own_future
            if duplicate_result is not None or duplicate_future is not None:
                result_text = duplicate_result if duplicate_result is not None else await duplicate_future
                if on_completed is not None:
                    await on_completed(name, call_index, True, False, time.perf_counter() - started_at, None, True)
                return result_text
            try:
                if raw_arguments is None:
                    raise ValueError("工具参数不是合法 JSON")
                validated = TOOL_INPUT_MODELS[name].model_validate(raw_arguments)
                result = await implementation.execute(validated.model_dump())
                if evidence is not None:
                    evidence.append({
                        "tool_name": name,
                        "arguments": validated.model_dump(),
                        "raw_arguments": arguments,
                        "result": result,
                        "succeeded": True,
                        "truncated": bool(result.get("truncated", False)) if isinstance(result, Mapping) else False,
                    })
                serialized = json.dumps(result, ensure_ascii=False, default=str)
                if cache_key is not None:
                    async with lock:
                        completed_results[cache_key] = serialized
                        in_flight.pop(cache_key, None)
                        if own_future is not None and not own_future.done():
                            own_future.set_result(serialized)
                if on_completed is not None:
                    await on_completed(name, call_index, True, False, time.perf_counter() - started_at, None, False)
                return serialized
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if evidence is not None:
                    evidence.append({
                        "tool_name": name,
                        "arguments": raw_arguments if isinstance(raw_arguments, dict) else {},
                        "raw_arguments": arguments,
                        "result": {"error": detail},
                        "succeeded": False,
                        "truncated": False,
                    })
                if cache_key is not None:
                    async with lock:
                        in_flight.pop(cache_key, None)
                        if own_future is not None and not own_future.done():
                            own_future.set_result(json.dumps({"error": detail}, ensure_ascii=False))
                if on_completed is not None:
                    await on_completed(name, call_index, False, False, time.perf_counter() - started_at, detail, False)
                return json.dumps({"error": detail}, ensure_ascii=False)

        return FunctionTool(
            name=name,
            description=_TOOL_DESCRIPTIONS[name],
            params_json_schema=_input_schema(TOOL_INPUT_MODELS[name]),
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    return [build(name, implementation) for name, implementation in implementations.items()]
