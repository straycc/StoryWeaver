"""Context V2 Snapshot 持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select

from .database import Database
from .tables import ContextSnapshotRow


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    snapshot_id: str
    job_id: str | None
    book_id: str | None
    agent_role: str
    book_version: int | None
    policy_version: str
    renderer_version: str
    rendered_context: str
    trace: Mapping[str, Any]
    created_at: str


class ContextSnapshotRepository:
    """保存 Agent 实际可见 Context；不承担业务状态写入。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save(
        self,
        *,
        agent_role: str,
        book_id: str | None,
        book_version: int | None,
        policy_version: str,
        renderer_version: str,
        rendered_context: str,
        trace: Mapping[str, Any],
        job_id: str | None = None,
    ) -> ContextSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        row = ContextSnapshotRow(
            snapshot_id=str(uuid4()), job_id=job_id, book_id=book_id,
            agent_role=agent_role, book_version=book_version,
            policy_version=policy_version, renderer_version=renderer_version,
            rendered_context=rendered_context, trace_json=dict(trace), created_at=now,
        )
        with self.database.session() as session:
            with session.begin():
                session.add(row)
        return self._decode(row)

    def list_for_book(self, book_id: str, *, limit: int = 50) -> tuple[ContextSnapshot, ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(ContextSnapshotRow)
                .where(ContextSnapshotRow.book_id == book_id)
                .order_by(ContextSnapshotRow.created_at.desc())
                .limit(limit)
            ).all()
        return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _decode(row: ContextSnapshotRow) -> ContextSnapshot:
        return ContextSnapshot(
            snapshot_id=row.snapshot_id, job_id=row.job_id, book_id=row.book_id,
            agent_role=row.agent_role, book_version=row.book_version,
            policy_version=row.policy_version, renderer_version=row.renderer_version,
            rendered_context=row.rendered_context, trace=dict(row.trace_json), created_at=row.created_at,
        )
