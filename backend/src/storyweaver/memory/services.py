"""长期记忆的自动提取、选择和低频整理。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..llm import LlmMessage, LlmMessageRole
from .long_term import (
    LongTermMemoryRecord,
    LongTermMemoryStatus,
    LongTermMemoryType,
    MemoryCandidate,
    MemoryExtractionResult,
    MemoryScopeType,
)
from .long_term_store import LongTermMemoryStore


def _response_text(response: object) -> str:
    output = getattr(response, "output", None)
    if isinstance(output, str) and output.strip():
        return output.strip()
    text = getattr(response, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _json_value(text: str, expected: type[list[Any]] | type[dict[str, Any]]) -> Any:
    """容忍 Markdown 代码围栏，但拒绝无法定位的自由文本。"""

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        opening, closing = ("[", "]") if expected is list else ("{", "}")
        start = text.find(opening)
        end = text.rfind(closing)
        if start < 0 or end <= start:
            raise ValueError("模型没有返回可解析的 JSON")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, expected):
        raise ValueError("模型 JSON 顶层类型不符合约定")
    return value


TextGenerator = Callable[[str], Awaitable[str]]


class LongTermMemoryExtractor:
    """从已完成的普通聊天回合中自动提取稳定信息。"""

    def __init__(self, *, generate_text: TextGenerator, store: LongTermMemoryStore) -> None:
        self.generate_text = generate_text
        self.store = store

    async def extract(
        self,
        *,
        user_message: str,
        assistant_message: str,
        session_id: str,
        user_message_id: str,
        book_id: str | None,
    ) -> MemoryExtractionResult:
        existing = self._available_records(book_id)[:200]
        catalog = "\n".join(
            f"- {item.memory_id} | {item.memory_type.value} | "
            f"{item.scope_type.value}:{item.scope_id} | {item.name} | {item.description}"
            for item in existing
        ) or "（暂无）"
        prompt = (
            "你是长期记忆提取器。只提取用户明确表达或明确确认、未来仍有用的信息。\n"
            "不要提取普通问题、一次性任务、助手自行提出但用户未确认的创意，也不要把聊天内容写成小说正史。\n"
            "返回 JSON 数组；没有新信息时返回 []。每项字段：\n"
            "memory_type: user_preference|feedback|project_directive|reference\n"
            "scope_type: global|book；通用偏好用 global，当前作品特有要求用 book\n"
            "name: 简短稳定标识；description: 一行目录摘要；content: 完整可执行说明；"
            "importance: 1-5；supersedes_id: 冲突旧记忆 ID 或 null。\n"
            f"当前 book_id：{book_id or '无'}\n\n已有记忆：\n{catalog}\n\n"
            f"用户：{user_message}\n助手：{assistant_message}"
        )
        try:
            items = _json_value(await self.generate_text(prompt), list)
            records: list[LongTermMemoryRecord] = []
            ignored = 0
            for raw in items:
                candidate = self._candidate(raw)
                if candidate is None:
                    ignored += 1
                    continue
                scope_id = "default" if candidate.scope_type == MemoryScopeType.GLOBAL else book_id
                if not scope_id:
                    ignored += 1
                    continue
                supersedes_id = self._resolve_supersedes(
                    candidate=candidate,
                    scope_id=scope_id,
                    existing=existing,
                )
                now = self.store.timestamp()
                record = LongTermMemoryRecord(
                    memory_id=str(uuid4()),
                    memory_type=candidate.memory_type,
                    scope_type=candidate.scope_type,
                    scope_id=scope_id,
                    name=candidate.name,
                    description=candidate.description,
                    content=candidate.content,
                    importance=candidate.importance,
                    source_refs=(
                        f"session:{session_id}",
                        f"message:{user_message_id}",
                    ),
                    fingerprint=self.store.fingerprint(candidate.content),
                    status=LongTermMemoryStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                    supersedes_id=supersedes_id,
                )
                if self.store.save(record):
                    records.append(record)
                else:
                    ignored += 1
            return MemoryExtractionResult(tuple(records), ignored_count=ignored)
        except Exception as exc:  # noqa: BLE001 - 记忆辅助调用不能破坏主回复
            return MemoryExtractionResult(error=f"{type(exc).__name__}: {exc}")

    def _available_records(self, book_id: str | None) -> list[LongTermMemoryRecord]:
        records = list(
            self.store.list_records(
                scope_type=MemoryScopeType.GLOBAL,
                scope_id="default",
                status=LongTermMemoryStatus.ACTIVE,
            )
        )
        if book_id:
            records.extend(
                self.store.list_records(
                    scope_type=MemoryScopeType.BOOK,
                    scope_id=book_id,
                    status=LongTermMemoryStatus.ACTIVE,
                )
            )
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    @staticmethod
    def _candidate(raw: object) -> MemoryCandidate | None:
        if not isinstance(raw, dict):
            return None
        try:
            return MemoryCandidate(
                memory_type=LongTermMemoryType(str(raw.get("memory_type", ""))),
                scope_type=MemoryScopeType(str(raw.get("scope_type", ""))),
                name=str(raw.get("name", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                content=str(raw.get("content", "")).strip(),
                importance=max(1, min(5, int(raw.get("importance", 3)))),
                supersedes_id=(
                    str(raw["supersedes_id"]).strip()
                    if raw.get("supersedes_id")
                    else None
                ),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _resolve_supersedes(
        *,
        candidate: MemoryCandidate,
        scope_id: str,
        existing: list[LongTermMemoryRecord],
    ) -> str | None:
        if candidate.supersedes_id:
            matched = next(
                (
                    item
                    for item in existing
                    if item.memory_id == candidate.supersedes_id
                    and item.scope_type == candidate.scope_type
                    and item.scope_id == scope_id
                ),
                None,
            )
            if matched:
                return matched.memory_id
        normalized_name = candidate.name.casefold().strip()
        matched = next(
            (
                item
                for item in existing
                if item.scope_type == candidate.scope_type
                and item.scope_id == scope_id
                and item.memory_type == candidate.memory_type
                and item.name.casefold().strip() == normalized_name
                and item.content != candidate.content
            ),
            None,
        )
        return matched.memory_id if matched else None


class LongTermMemoryRetriever:
    """作用域过滤后，让 LLM 从目录中最多选择五条记忆。"""

    def __init__(
        self,
        *,
        generate_text: TextGenerator,
        store: LongTermMemoryStore,
        max_catalog_items: int = 200,
        limit: int = 5,
    ) -> None:
        self.generate_text = generate_text
        self.store = store
        self.max_catalog_items = max_catalog_items
        self.limit = limit

    async def retrieve(
        self,
        *,
        query: str,
        book_id: str | None,
    ) -> tuple[LongTermMemoryRecord, ...]:
        records = list(
            self.store.list_records(
                scope_type=MemoryScopeType.GLOBAL,
                scope_id="default",
                status=LongTermMemoryStatus.ACTIVE,
            )
        )
        if book_id:
            records.extend(
                self.store.list_records(
                    scope_type=MemoryScopeType.BOOK,
                    scope_id=book_id,
                    status=LongTermMemoryStatus.ACTIVE,
                )
            )
        records.sort(key=lambda item: item.updated_at, reverse=True)
        records = records[: self.max_catalog_items]
        if not records or not query.strip():
            return ()
        catalog = "\n".join(
            f"{item.memory_id} | {item.name} | {item.description}"
            for item in records
        )
        prompt = (
            "根据当前请求，从长期记忆目录选择明确相关的记忆。"
            f"最多返回 {self.limit} 个 memory_id，只返回 JSON 字符串数组；"
            "没有相关项返回 []。\n\n"
            f"当前请求：{query}\n\n记忆目录：\n{catalog}"
        )
        try:
            selected_ids = _json_value(await self.generate_text(prompt), list)
            index = {item.memory_id: item for item in records}
            selected: list[LongTermMemoryRecord] = []
            for memory_id in selected_ids:
                item = index.get(str(memory_id))
                if item and item not in selected:
                    selected.append(item)
                if len(selected) >= self.limit:
                    break
            return tuple(selected)
        except Exception:
            return self._fallback(records, query)

    def load_selected(
        self,
        *,
        memory_ids: tuple[str, ...],
        book_id: str | None,
    ) -> tuple[LongTermMemoryRecord, ...]:
        """按已确认顺序恢复仍然有效且作用域匹配的长期记忆。"""

        if not memory_ids:
            return ()
        records = list(
            self.store.list_records(
                scope_type=MemoryScopeType.GLOBAL,
                scope_id="default",
                status=LongTermMemoryStatus.ACTIVE,
            )
        )
        if book_id:
            records.extend(
                self.store.list_records(
                    scope_type=MemoryScopeType.BOOK,
                    scope_id=book_id,
                    status=LongTermMemoryStatus.ACTIVE,
                )
            )
        index = {item.memory_id: item for item in records}
        return tuple(index[memory_id] for memory_id in memory_ids if memory_id in index)

    def _fallback(
        self,
        records: list[LongTermMemoryRecord],
        query: str,
    ) -> tuple[LongTermMemoryRecord, ...]:
        query_terms = self._terms(query)
        scored = []
        for item in records:
            overlap = len(
                query_terms
                & self._terms(f"{item.name} {item.description} {item.content}")
            )
            scored.append((overlap, item.importance, item.updated_at, item))
        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        relevant = [row[3] for row in scored if row[0] > 0]
        return tuple(relevant[: self.limit])

    @staticmethod
    def _terms(text: str) -> set[str]:
        normalized = text.casefold()
        terms = set(re.findall(r"[a-z0-9_]+", normalized))
        for block in re.findall(r"[\u3400-\u9fff]+", normalized):
            if len(block) == 1:
                terms.add(block)
            else:
                terms.update(block[index : index + 2] for index in range(len(block) - 1))
        return terms


class LongTermMemoryConsolidator:
    """每个作用域累计十条活跃记忆后，低频合并明显重复项。"""

    def __init__(self, *, generate_text: TextGenerator, store: LongTermMemoryStore) -> None:
        self.generate_text = generate_text
        self.store = store

    async def consolidate_if_needed(
        self,
        *,
        scope_type: MemoryScopeType,
        scope_id: str,
    ) -> int:
        records = self.store.list_records(
            scope_type=scope_type,
            scope_id=scope_id,
            status=LongTermMemoryStatus.ACTIVE,
        )
        if len(records) < 10:
            return 0
        catalog = "\n\n".join(
            f"ID: {item.memory_id}\n名称: {item.name}\n摘要: {item.description}\n内容: {item.content}"
            for item in records
        )
        prompt = (
            "找出长期记忆中语义重复或被更精确记忆替代的条目。"
            "不要合并仅仅相关但含义不同的条目。返回 JSON 数组，"
            "每项为 {keep_id, supersede_ids}；没有可整理项返回 []。\n\n"
            + catalog
        )
        changed = 0
        try:
            groups = _json_value(await self.generate_text(prompt), list)
            active_ids = {item.memory_id for item in records}
            for group in groups:
                if not isinstance(group, dict):
                    continue
                keep_id = str(group.get("keep_id", ""))
                if keep_id not in active_ids:
                    continue
                for memory_id in group.get("supersede_ids", []):
                    memory_id = str(memory_id)
                    if memory_id in active_ids and memory_id != keep_id:
                        self.store.update(
                            memory_id,
                            status=LongTermMemoryStatus.SUPERSEDED,
                            supersedes_id=None,
                        )
                        changed += 1
        except Exception:
            return 0
        return changed
