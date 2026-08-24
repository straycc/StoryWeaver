"""结构化角色记忆。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryType(StrEnum):
    """首版支持的记忆类型。"""

    EPISODIC = "episodic"
    PRIVATE = "private"
    WORLD_FACT = "world_fact"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """某个角色对故事信息的主观记录。"""

    memory_id: str
    session_id: str
    owner_id: str
    content: str
    memory_type: MemoryType
    importance: int
    created_sequence: int
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValueError("memory_id 不能为空")
        if not self.session_id.strip():
            raise ValueError("session_id 不能为空")
        if not self.owner_id.strip():
            raise ValueError("owner_id 不能为空")
        if not self.content.strip():
            raise ValueError("记忆内容不能为空")
        if not 1 <= self.importance <= 5:
            raise ValueError("记忆 importance 必须在 1 到 5 之间")
        if self.created_sequence < 0:
            raise ValueError("记忆 created_sequence 不能小于 0")
