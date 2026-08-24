"""把权威事件投影为角色记忆，并按上下文需要检索。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Protocol
from uuid import uuid4

from .record import MemoryRecord, MemoryType
from .store import InMemoryMemoryStore, MemoryStore

class NarrativeEvent(Protocol):
    """可投影到记忆的最小叙事事件契约。

    它不依赖任何特定交互运行时，未来人物沙盒可以通过
    同名字段的领域事件接入，而不反向耦合记忆模块。
    """

    event_id: str
    session_id: str
    sequence: int
    summary: str
    payload: dict[str, Any]

    @property
    def visibility(self) -> Any: ...


class MemoryManager:
    """管理角色记忆的写入、去重和有限检索。"""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store if store is not None else InMemoryMemoryStore()

    def remember_private(
        self,
        *,
        session_id: str,
        owner_id: str,
        content: str,
        created_sequence: int = 0,
        source_event_id: str | None = None,
        importance: int = 4,
    ) -> bool:
        normalized_content = content.strip()
        if not normalized_content:
            return False
        return self.store.add(
            MemoryRecord(
                memory_id=str(uuid4()),
                session_id=session_id,
                owner_id=owner_id,
                content=normalized_content,
                memory_type=MemoryType.PRIVATE,
                importance=importance,
                created_sequence=created_sequence,
                source_event_id=source_event_id,
            )
        )

    def project_event(
        self,
        *,
        event: NarrativeEvent,
        recipient_ids: Iterable[str],
    ) -> None:
        """只向有权看到该事件的指定角色写入情节记忆。"""

        unique_recipient_ids = dict.fromkeys(recipient_ids)
        for owner_id in unique_recipient_ids:
            if not event.visibility.can_view(owner_id):
                continue
            self._remember_event_for_owner(event=event, owner_id=owner_id)

    def retrieve(
        self,
        *,
        session_id: str,
        owner_id: str,
        query: str = "",
        limit: int = 8,
        recent_limit: int = 4,
        important_limit: int = 3,
        relevant_limit: int = 3,
    ) -> tuple[MemoryRecord, ...]:
        """组合近期、重要和相关记忆，且不返回无限历史。"""

        if limit < 1:
            raise ValueError("记忆检索 limit 必须大于 0")
        memories = self.store.list_for_owner(session_id, owner_id)
        if not memories:
            return ()

        selected: dict[str, MemoryRecord] = {}

        def include(items: Iterable[MemoryRecord]) -> None:
            for item in items:
                if len(selected) >= limit:
                    return
                selected.setdefault(item.memory_id, item)

        include(reversed(memories[-recent_limit:]))
        important = sorted(
            memories,
            key=lambda item: (item.importance, item.created_sequence),
            reverse=True,
        )
        include(important[:important_limit])

        if query.strip():
            relevant = sorted(
                memories,
                key=lambda item: (
                    self._relevance(item.content, query),
                    item.importance,
                    item.created_sequence,
                ),
                reverse=True,
            )
            include(
                item
                for item in relevant[:relevant_limit]
                if self._relevance(item.content, query) > 0
            )

        return tuple(
            sorted(selected.values(), key=lambda item: item.created_sequence)
        )

    def _remember_event_for_owner(self, *, event: NarrativeEvent, owner_id: str) -> None:
        if not event.summary.strip():
            return
        progress_score = event.payload.get("director_progress_score", 1)
        importance = progress_score if isinstance(progress_score, int) else 1
        importance = max(1, min(5, importance))
        self.store.add(
            MemoryRecord(
                memory_id=str(uuid4()),
                session_id=event.session_id,
                owner_id=owner_id,
                content=event.summary,
                memory_type=MemoryType.EPISODIC,
                importance=importance,
                created_sequence=event.sequence,
                source_event_id=event.event_id,
            )
        )

        new_fact = event.payload.get("new_fact")
        if isinstance(new_fact, str) and new_fact.strip():
            self.store.add(
                MemoryRecord(
                    memory_id=str(uuid4()),
                    session_id=event.session_id,
                    owner_id=owner_id,
                    content=new_fact.strip(),
                    memory_type=MemoryType.WORLD_FACT,
                    importance=5,
                    created_sequence=event.sequence,
                    source_event_id=event.event_id,
                )
            )

    @classmethod
    def _relevance(cls, content: str, query: str) -> int:
        return len(cls._terms(content) & cls._terms(query))

    @staticmethod
    def _terms(text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", text.casefold())
        words = set(re.findall(r"[a-z0-9_]+", normalized))
        chinese_blocks = re.findall(r"[\u4e00-\u9fff]+", normalized)
        for block in chinese_blocks:
            words.update(block[index : index + 2] for index in range(len(block) - 1))
            if len(block) == 1:
                words.add(block)
        return words
