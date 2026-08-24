"""一条记忆一个 JSON 的长期记忆持久层。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .long_term import (
    LongTermMemoryRecord,
    LongTermMemoryStatus,
    LongTermMemoryType,
    MemoryScopeType,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class LongTermMemoryStore(Protocol):
    def save(self, record: LongTermMemoryRecord) -> bool: ...

    def get(self, memory_id: str) -> LongTermMemoryRecord: ...

    def list_records(
        self,
        *,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
        status: LongTermMemoryStatus | None = None,
    ) -> tuple[LongTermMemoryRecord, ...]: ...

    def update(self, memory_id: str, **changes: object) -> LongTermMemoryRecord: ...


class JsonLongTermMemoryStore:
    """本地单用户首版 Store，写入采用同目录原子替换。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    @staticmethod
    def fingerprint(content: str) -> str:
        normalized = re.sub(r"[^\w\u3400-\u9fff]+", "", content).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def save(self, record: LongTermMemoryRecord) -> bool:
        with self._lock:
            duplicates = self.list_records(
                scope_type=record.scope_type,
                scope_id=record.scope_id,
                status=LongTermMemoryStatus.ACTIVE,
            )
            if any(item.fingerprint == record.fingerprint for item in duplicates):
                return False
            if record.supersedes_id:
                previous = self.get(record.supersedes_id)
                if (
                    previous.scope_type != record.scope_type
                    or previous.scope_id != record.scope_id
                ):
                    raise ValueError("替代记忆必须位于同一作用域")
                self.update(
                    previous.memory_id,
                    status=LongTermMemoryStatus.SUPERSEDED,
                )
            self._write(record)
            return True

    def get(self, memory_id: str) -> LongTermMemoryRecord:
        self._validate_id(memory_id, "memory_id")
        for path in self.root.glob(f"**/{memory_id}.json"):
            if path.is_file():
                return self._decode(self._read_json(path))
        raise KeyError(f"长期记忆不存在：{memory_id}")

    def list_records(
        self,
        *,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
        status: LongTermMemoryStatus | None = None,
    ) -> tuple[LongTermMemoryRecord, ...]:
        if scope_id is not None:
            self._validate_id(scope_id, "scope_id")
        if not self.root.exists():
            return ()
        records: list[LongTermMemoryRecord] = []
        for path in self.root.glob("**/*.json"):
            if path.name.startswith("_"):
                continue
            record = self._decode(self._read_json(path))
            if scope_type is not None and record.scope_type != scope_type:
                continue
            if scope_id is not None and record.scope_id != scope_id:
                continue
            if status is not None and record.status != status:
                continue
            records.append(record)
        return tuple(
            sorted(records, key=lambda item: (item.updated_at, item.memory_id), reverse=True)
        )

    def update(self, memory_id: str, **changes: object) -> LongTermMemoryRecord:
        allowed = {
            "memory_type",
            "name",
            "description",
            "content",
            "importance",
            "status",
            "supersedes_id",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("不允许修改字段：" + ", ".join(sorted(unknown)))
        with self._lock:
            current = self.get(memory_id)
            normalized = dict(changes)
            if "memory_type" in normalized and isinstance(normalized["memory_type"], str):
                normalized["memory_type"] = LongTermMemoryType(normalized["memory_type"])
            if "status" in normalized and isinstance(normalized["status"], str):
                normalized["status"] = LongTermMemoryStatus(normalized["status"])
            if "content" in normalized:
                normalized["fingerprint"] = self.fingerprint(str(normalized["content"]))
            normalized["updated_at"] = self.timestamp()
            updated = replace(current, **normalized)
            self._write(updated)
            return updated

    def disable(self, memory_id: str) -> LongTermMemoryRecord:
        return self.update(memory_id, status=LongTermMemoryStatus.DISABLED)

    def restore(self, memory_id: str) -> LongTermMemoryRecord:
        with self._lock:
            current = self.get(memory_id)
            records = self.list_records(
                scope_type=current.scope_type,
                scope_id=current.scope_id,
                status=LongTermMemoryStatus.ACTIVE,
            )
            # 恢复旧版本时停用其当前继任者，避免冲突版本同时生效。
            for item in records:
                if item.supersedes_id == current.memory_id:
                    self.update(item.memory_id, status=LongTermMemoryStatus.DISABLED)
            # 恢复新版本时仍维持它对旧版本的替代关系。
            if current.supersedes_id:
                previous = self.get(current.supersedes_id)
                if previous.status == LongTermMemoryStatus.ACTIVE:
                    self.update(
                        previous.memory_id,
                        status=LongTermMemoryStatus.SUPERSEDED,
                    )
            return self.update(memory_id, status=LongTermMemoryStatus.ACTIVE)

    def _write(self, record: LongTermMemoryRecord) -> None:
        directory = self._scope_directory(record.scope_type, record.scope_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record.memory_id}.json"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(self._encode(record), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _scope_directory(
        self,
        scope_type: MemoryScopeType,
        scope_id: str,
    ) -> Path:
        self._validate_id(scope_id, "scope_id")
        if scope_type == MemoryScopeType.GLOBAL:
            return self.root / "global"
        return self.root / "books" / scope_id

    @staticmethod
    def _validate_id(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise ValueError(f"{field_name} 格式不合法")

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取长期记忆 {path}：{exc}") from exc

    @staticmethod
    def _encode(record: LongTermMemoryRecord) -> dict[str, Any]:
        return {
            "memory_id": record.memory_id,
            "memory_type": record.memory_type.value,
            "scope_type": record.scope_type.value,
            "scope_id": record.scope_id,
            "name": record.name,
            "description": record.description,
            "content": record.content,
            "importance": record.importance,
            "source_refs": list(record.source_refs),
            "fingerprint": record.fingerprint,
            "status": record.status.value,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "supersedes_id": record.supersedes_id,
        }

    @staticmethod
    def _decode(data: object) -> LongTermMemoryRecord:
        if not isinstance(data, dict):
            raise ValueError("长期记忆文件必须是 JSON 对象")
        expected = {
            "memory_id",
            "memory_type",
            "scope_type",
            "scope_id",
            "name",
            "description",
            "content",
            "importance",
            "source_refs",
            "fingerprint",
            "status",
            "created_at",
            "updated_at",
            "supersedes_id",
        }
        if set(data) != expected:
            raise ValueError("长期记忆字段不完整或包含未知字段")
        return LongTermMemoryRecord(
            memory_id=str(data["memory_id"]),
            memory_type=LongTermMemoryType(str(data["memory_type"])),
            scope_type=MemoryScopeType(str(data["scope_type"])),
            scope_id=str(data["scope_id"]),
            name=str(data["name"]),
            description=str(data["description"]),
            content=str(data["content"]),
            importance=int(data["importance"]),
            source_refs=tuple(str(item) for item in data["source_refs"]),
            fingerprint=str(data["fingerprint"]),
            status=LongTermMemoryStatus(str(data["status"])),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            supersedes_id=(
                str(data["supersedes_id"])
                if data["supersedes_id"] is not None
                else None
            ),
        )
