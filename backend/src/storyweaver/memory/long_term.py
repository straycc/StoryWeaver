"""跨会话长期记忆的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class LongTermMemoryType(StrEnum):
    USER_PREFERENCE = "user_preference"
    FEEDBACK = "feedback"
    PROJECT_DIRECTIVE = "project_directive"
    REFERENCE = "reference"


class MemoryScopeType(StrEnum):
    GLOBAL = "global"
    BOOK = "book"


class LongTermMemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class LongTermMemoryRecord:
    """一条可审计、可停用并可替代的长期记忆。"""

    memory_id: str
    memory_type: LongTermMemoryType
    scope_type: MemoryScopeType
    scope_id: str
    name: str
    description: str
    content: str
    importance: int
    source_refs: tuple[str, ...]
    fingerprint: str
    status: LongTermMemoryStatus
    created_at: str
    updated_at: str
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "memory_id",
            "scope_id",
            "name",
            "description",
            "content",
            "fingerprint",
            "created_at",
            "updated_at",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必须是非空字符串")
        if not 1 <= self.importance <= 5:
            raise ValueError("importance 必须在 1 到 5 之间")
        if not isinstance(self.source_refs, tuple):
            raise TypeError("source_refs 必须是 tuple")
        if self.scope_type == MemoryScopeType.GLOBAL and self.scope_id != "default":
            raise ValueError("global 记忆的 scope_id 必须是 default")
        if self.supersedes_id is not None and not self.supersedes_id.strip():
            raise ValueError("supersedes_id 必须是非空字符串或 None")


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """模型提取后、尚未写入 Store 的候选记忆。"""

    memory_type: LongTermMemoryType
    scope_type: MemoryScopeType
    name: str
    description: str
    content: str
    importance: int = 3
    supersedes_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryExtractionResult:
    records: tuple[LongTermMemoryRecord, ...] = ()
    error: str | None = None
    ignored_count: int = 0

