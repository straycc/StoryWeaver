"""持久 Job 与事件仓储。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..novel_creation.exceptions import BookBusyError
from .database import Database
from .tables import JobEventRow, JobRow


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "paused", "interrupted"})
LOCK_SCOPES = frozenset({"none", "book_read", "book_write"})


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    job_type: str
    book_id: str | None
    lock_scope: str
    status: str
    payload: Mapping[str, Any]
    result: Mapping[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobEvent:
    sequence: int
    job_id: str
    event_type: str
    created_at: str
    payload: Mapping[str, Any]


class JobRepository:
    """Job、事件与数据库级作品单活跃任务约束。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        job_type: str,
        book_id: str | None,
        payload: Mapping[str, Any],
        lock_scope: str = "none",
    ) -> Job:
        if lock_scope not in LOCK_SCOPES:
            raise ValueError(f"不支持的 Job 锁范围：{lock_scope}")
        now = self._now()
        row = JobRow(
            job_id=str(uuid4()), job_type=job_type, book_id=book_id, lock_scope=lock_scope, status="queued",
            payload_json=dict(payload), result_json=None, error=None,
            created_at=now, updated_at=now,
        )
        with self.database.session() as session:
            try:
                with session.begin():
                    session.add(row)
                    session.flush()
                    self._append(session, row.job_id, "job_queued", {
                        "job_type": job_type,
                        "book_id": book_id,
                        "lock_scope": lock_scope,
                    })
            except IntegrityError as exc:
                if book_id is not None and lock_scope == "book_write":
                    raise BookBusyError(f"作品 {book_id} 已有规划或写作任务正在执行") from exc
                raise
        return self.get(row.job_id)

    def get(self, job_id: str) -> Job:
        with self.database.session() as session:
            row = session.get(JobRow, job_id)
        if row is None:
            raise KeyError(f"Job 不存在：{job_id}")
        return self._decode(row)

    def start(self, job_id: str) -> Job:
        return self._transition(job_id, "running", event_type="job_started")

    def succeed(self, job_id: str, *, result: Mapping[str, Any] | None = None) -> Job:
        return self._transition(job_id, "succeeded", result=result, event_type="job_succeeded")

    def fail(self, job_id: str, *, error: str, status: str = "failed") -> Job:
        if status not in {"failed", "paused", "interrupted"}:
            raise ValueError("Job 失败状态不合法")
        return self._transition(job_id, status, error=error, event_type=f"job_{status}")

    def append_event(self, job_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> JobEvent:
        with self.database.session() as session:
            with session.begin():
                if session.get(JobRow, job_id) is None:
                    raise KeyError(f"Job 不存在：{job_id}")
                return self._append(session, job_id, event_type, dict(payload or {}))

    def events_after(self, job_id: str, *, after_sequence: int = 0) -> tuple[JobEvent, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(JobEventRow).where(
                JobEventRow.job_id == job_id,
                JobEventRow.sequence > after_sequence,
            ).order_by(JobEventRow.sequence)).all()
        return tuple(self._decode_event(row) for row in rows)

    def interrupt_active_jobs(self) -> int:
        """服务启动时标记未完成 Job；不会虚构恢复模型调用。"""

        now = self._now()
        with self.database.session() as session:
            with session.begin():
                rows = session.scalars(select(JobRow).where(JobRow.status.in_(ACTIVE_JOB_STATUSES)).with_for_update()).all()
                for row in rows:
                    row.status = "interrupted"
                    row.error = "服务重启导致模型调用中断；请显式重试该任务"
                    row.updated_at = now
                    self._append(session, row.job_id, "job_interrupted", {"error": row.error})
                return len(rows)

    def _transition(
        self,
        job_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        event_type: str,
    ) -> Job:
        with self.database.session() as session:
            with session.begin():
                row = session.get(JobRow, job_id, with_for_update=True)
                if row is None:
                    raise KeyError(f"Job 不存在：{job_id}")
                row.status = status
                row.result_json = dict(result) if result is not None else row.result_json
                row.error = error
                row.updated_at = self._now()
                self._append(session, job_id, event_type, {
                    **({"result": dict(result)} if result is not None else {}),
                    **({"error": error} if error else {}),
                })
                session.flush()
                return self._decode(row)

    def _append(self, session: Any, job_id: str, event_type: str, payload: Mapping[str, Any]) -> JobEvent:
        sequence = int(session.scalar(select(func.coalesce(func.max(JobEventRow.sequence), 0)).where(JobEventRow.job_id == job_id)) or 0) + 1
        row = JobEventRow(
            job_id=job_id, sequence=sequence, event_type=event_type,
            created_at=self._now(), payload_json=dict(payload),
        )
        session.add(row)
        return self._decode_event(row)

    @staticmethod
    def _decode(row: JobRow) -> Job:
        return Job(
            job_id=row.job_id, job_type=row.job_type, book_id=row.book_id,
            lock_scope=row.lock_scope, status=row.status,
            payload=dict(row.payload_json), result=dict(row.result_json) if row.result_json is not None else None,
            error=row.error, created_at=row.created_at, updated_at=row.updated_at,
        )

    @staticmethod
    def _decode_event(row: JobEventRow) -> JobEvent:
        return JobEvent(
            sequence=row.sequence, job_id=row.job_id, event_type=row.event_type,
            created_at=row.created_at, payload=dict(row.payload_json),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
