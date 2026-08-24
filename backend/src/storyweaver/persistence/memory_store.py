"""PostgreSQL 长期记忆仓储。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.long_term import LongTermMemoryRecord, LongTermMemoryStatus, LongTermMemoryType, MemoryScopeType
from ..memory.long_term_store import JsonLongTermMemoryStore
from .database import Database
from .tables import LongTermMemoryRow


class PostgresLongTermMemoryStore:
    """沿用 LongTermMemoryStore 协议，数据仅落 PostgreSQL。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def timestamp() -> str:
        """与 LongTermMemoryStore 协议保持一致。"""

        return JsonLongTermMemoryStore.timestamp()

    @staticmethod
    def fingerprint(content: str) -> str:
        """与文件实现保持同一去重算法。"""

        return JsonLongTermMemoryStore.fingerprint(content)

    def save(self, record: LongTermMemoryRecord) -> bool:
        with self.database.session() as session:
            try:
                with session.begin():
                    existing = session.scalar(select(LongTermMemoryRow).where(
                        LongTermMemoryRow.scope_type == record.scope_type.value,
                        LongTermMemoryRow.scope_id == record.scope_id,
                        LongTermMemoryRow.fingerprint == record.fingerprint,
                        LongTermMemoryRow.status == LongTermMemoryStatus.ACTIVE.value,
                    ))
                    if existing is not None:
                        return False
                    if record.supersedes_id:
                        previous = self._require(session, record.supersedes_id)
                        if previous.scope_type != record.scope_type.value or previous.scope_id != record.scope_id:
                            raise ValueError("替代记忆必须位于同一作用域")
                        previous.status = LongTermMemoryStatus.SUPERSEDED.value
                        previous.record_json = self._encode(replace(self._decode(previous.record_json), status=LongTermMemoryStatus.SUPERSEDED, updated_at=JsonLongTermMemoryStore.timestamp()))
                    session.add(LongTermMemoryRow(
                        memory_id=record.memory_id, scope_type=record.scope_type.value,
                        scope_id=record.scope_id, status=record.status.value,
                        fingerprint=record.fingerprint, record_json=self._encode(record),
                    ))
                    return True
            except IntegrityError:
                return False

    def get(self, memory_id: str) -> LongTermMemoryRecord:
        with self.database.session() as session:
            row = self._require(session, memory_id)
            return self._decode(row.record_json)

    def list_records(self, *, scope_type: MemoryScopeType | None = None, scope_id: str | None = None, status: LongTermMemoryStatus | None = None) -> tuple[LongTermMemoryRecord, ...]:
        statement = select(LongTermMemoryRow)
        if scope_type is not None:
            statement = statement.where(LongTermMemoryRow.scope_type == scope_type.value)
        if scope_id is not None:
            statement = statement.where(LongTermMemoryRow.scope_id == scope_id)
        if status is not None:
            statement = statement.where(LongTermMemoryRow.status == status.value)
        with self.database.session() as session:
            rows = session.scalars(statement).all()
        return tuple(sorted((self._decode(row.record_json) for row in rows), key=lambda item: (item.updated_at, item.memory_id), reverse=True))

    def update(self, memory_id: str, **changes: object) -> LongTermMemoryRecord:
        allowed = {"memory_type", "name", "description", "content", "importance", "status", "supersedes_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("不允许修改字段：" + ", ".join(sorted(unknown)))
        with self.database.session() as session:
            with session.begin():
                row = self._require(session, memory_id)
                current = self._decode(row.record_json)
                normalized = dict(changes)
                if isinstance(normalized.get("memory_type"), str):
                    normalized["memory_type"] = LongTermMemoryType(str(normalized["memory_type"]))
                if isinstance(normalized.get("status"), str):
                    normalized["status"] = LongTermMemoryStatus(str(normalized["status"]))
                if "content" in normalized:
                    normalized["fingerprint"] = JsonLongTermMemoryStore.fingerprint(str(normalized["content"]))
                normalized["updated_at"] = JsonLongTermMemoryStore.timestamp()
                updated = replace(current, **normalized)
                row.scope_type, row.scope_id, row.status, row.fingerprint = updated.scope_type.value, updated.scope_id, updated.status.value, updated.fingerprint
                row.record_json = self._encode(updated)
                return updated

    def disable(self, memory_id: str) -> LongTermMemoryRecord:
        return self.update(memory_id, status=LongTermMemoryStatus.DISABLED)

    def restore(self, memory_id: str) -> LongTermMemoryRecord:
        current = self.get(memory_id)
        for item in self.list_records(scope_type=current.scope_type, scope_id=current.scope_id, status=LongTermMemoryStatus.ACTIVE):
            if item.supersedes_id == current.memory_id:
                self.update(item.memory_id, status=LongTermMemoryStatus.DISABLED)
        if current.supersedes_id:
            previous = self.get(current.supersedes_id)
            if previous.status == LongTermMemoryStatus.ACTIVE:
                self.update(previous.memory_id, status=LongTermMemoryStatus.SUPERSEDED)
        return self.update(memory_id, status=LongTermMemoryStatus.ACTIVE)

    @staticmethod
    def _require(session: Any, memory_id: str) -> LongTermMemoryRow:
        row = session.get(LongTermMemoryRow, memory_id)
        if row is None:
            raise KeyError(f"长期记忆不存在：{memory_id}")
        return row

    @staticmethod
    def _encode(record: LongTermMemoryRecord) -> dict[str, Any]:
        return JsonLongTermMemoryStore._encode(record)

    @staticmethod
    def _decode(data: object) -> LongTermMemoryRecord:
        return JsonLongTermMemoryStore._decode(data)
