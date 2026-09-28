"""长期记忆的领域端口与稳定序列化工具。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from .long_term import LongTermMemoryRecord, LongTermMemoryStatus, LongTermMemoryType, MemoryScopeType


class LongTermMemoryStore(Protocol):
    """正式运行时由 SQLite 实现的长期记忆接口。"""

    def save(self, record: LongTermMemoryRecord) -> bool: ...
    def get(self, memory_id: str) -> LongTermMemoryRecord: ...
    def list_records(self, *, scope_type: MemoryScopeType | None = None, scope_id: str | None = None, status: LongTermMemoryStatus | None = None) -> tuple[LongTermMemoryRecord, ...]: ...
    def update(self, memory_id: str, **changes: object) -> LongTermMemoryRecord: ...
    def disable(self, memory_id: str) -> LongTermMemoryRecord: ...
    def restore(self, memory_id: str) -> LongTermMemoryRecord: ...
    def delete(self, memory_id: str) -> None: ...


def memory_fingerprint(content: str) -> str:
    """生成与历史数据兼容的归一化去重指纹。"""

    normalized = re.sub(r"[^\w\u3400-\u9fff]+", "", content).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def memory_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_long_term_memory(record: LongTermMemoryRecord) -> dict[str, Any]:
    return {
        "memory_id": record.memory_id, "memory_type": record.memory_type.value,
        "scope_type": record.scope_type.value, "scope_id": record.scope_id,
        "name": record.name, "description": record.description,
        "content": record.content, "importance": record.importance,
        "source_refs": list(record.source_refs), "fingerprint": record.fingerprint,
        "status": record.status.value, "created_at": record.created_at,
        "updated_at": record.updated_at, "supersedes_id": record.supersedes_id,
    }


def decode_long_term_memory(data: object) -> LongTermMemoryRecord:
    if not isinstance(data, dict):
        raise ValueError("长期记忆记录必须是 JSON 对象")
    expected = {
        "memory_id", "memory_type", "scope_type", "scope_id", "name", "description",
        "content", "importance", "source_refs", "fingerprint", "status", "created_at",
        "updated_at", "supersedes_id",
    }
    if set(data) != expected:
        raise ValueError("长期记忆字段不完整或包含未知字段")
    return LongTermMemoryRecord(
        memory_id=str(data["memory_id"]), memory_type=LongTermMemoryType(str(data["memory_type"])),
        scope_type=MemoryScopeType(str(data["scope_type"])), scope_id=str(data["scope_id"]),
        name=str(data["name"]), description=str(data["description"]), content=str(data["content"]),
        importance=int(data["importance"]), source_refs=tuple(str(item) for item in data["source_refs"]),
        fingerprint=str(data["fingerprint"]), status=LongTermMemoryStatus(str(data["status"])),
        created_at=str(data["created_at"]), updated_at=str(data["updated_at"]),
        supersedes_id=str(data["supersedes_id"]) if data["supersedes_id"] is not None else None,
    )
