"""作品与会话的原子删除策略。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .database import Database
from .jobs import ACTIVE_JOB_STATUSES
from .tables import (
    ActionProposalRow,
    BookRow,
    ChapterRow,
    ChapterRunRow,
    ChatSessionEventRow,
    ChatSessionRow,
    ContextSnapshotRow,
    JobEventRow,
    JobRow,
    LongTermMemoryRow,
    SimulationSessionRow,
    SimulationTurnRow,
)


class DeletionConflictError(RuntimeError):
    """目标仍被后台任务使用，当前不能安全删除。"""


@dataclass(frozen=True, slots=True)
class BookDeletionResult:
    """作品删除结果；对话不会随作品一起丢失。"""

    book_id: str
    unbound_session_ids: tuple[str, ...]


class PostgresDeletionRepository:
    """集中处理跨聚合删除，避免 API 层拼接多个非原子仓储调用。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def delete_session(self, session_id: str) -> None:
        """删除会话及其事件、操作提案，但保留已提取的长期记忆。"""

        with self.database.session() as session:
            with session.begin():
                row = session.get(ChatSessionRow, session_id, with_for_update=True)
                if row is None:
                    raise KeyError(f"会话不存在：{session_id}")
                active_jobs = session.scalars(
                    select(JobRow).where(JobRow.status.in_(ACTIVE_JOB_STATUSES))
                ).all()
                if any(item.payload_json.get("session_id") == session_id for item in active_jobs):
                    raise DeletionConflictError("当前会话仍有任务正在执行，请等待任务结束或取消任务后再删除")

                # 显式删除依赖，使 SQLite 测试和 PostgreSQL 的行为保持一致。
                session.execute(delete(ActionProposalRow).where(ActionProposalRow.session_id == session_id))
                session.execute(delete(ChatSessionEventRow).where(ChatSessionEventRow.session_id == session_id))
                session.delete(row)

    def delete_book(self, book_id: str) -> BookDeletionResult:
        """删除作品域数据，并把历史对话安全地改为未绑定状态。"""

        with self.database.session() as session:
            with session.begin():
                book = session.get(BookRow, book_id, with_for_update=True)
                if book is None:
                    raise KeyError(f"项目不存在：{book_id}")
                active_job = session.scalar(select(JobRow).where(
                    JobRow.book_id == book_id,
                    JobRow.status.in_(ACTIVE_JOB_STATUSES),
                ).limit(1))
                if active_job is not None:
                    raise DeletionConflictError("当前作品仍有任务正在执行，请等待任务结束或取消任务后再删除")

                bound_sessions = session.scalars(
                    select(ChatSessionRow)
                    .where(ChatSessionRow.book_id == book_id)
                    .order_by(ChatSessionRow.created_at)
                    .with_for_update()
                ).all()
                for chat_session in bound_sessions:
                    self._append_unbind_event(session, chat_session)

                # 下列字段没有全部建立数据库外键，因此在同一事务中显式清理。
                session.execute(delete(LongTermMemoryRow).where(
                    LongTermMemoryRow.scope_type == "book",
                    LongTermMemoryRow.scope_id == book_id,
                ))
                session.execute(delete(ActionProposalRow).where(ActionProposalRow.book_id == book_id))

                simulation_ids = select(SimulationSessionRow.simulation_id).where(
                    SimulationSessionRow.book_id == book_id
                )
                session.execute(delete(SimulationTurnRow).where(
                    SimulationTurnRow.simulation_id.in_(simulation_ids)
                ))
                session.execute(delete(SimulationSessionRow).where(SimulationSessionRow.book_id == book_id))
                session.execute(delete(ContextSnapshotRow).where(ContextSnapshotRow.book_id == book_id))
                session.execute(delete(ChapterRow).where(ChapterRow.book_id == book_id))
                session.execute(delete(ChapterRunRow).where(ChapterRunRow.book_id == book_id))

                job_ids = select(JobRow.job_id).where(JobRow.book_id == book_id)
                session.execute(delete(JobEventRow).where(JobEventRow.job_id.in_(job_ids)))
                session.execute(delete(JobRow).where(JobRow.book_id == book_id))
                session.delete(book)

        return BookDeletionResult(
            book_id=book_id,
            unbound_session_ids=tuple(item.session_id for item in bound_sessions),
        )

    @staticmethod
    def _append_unbind_event(session: Session, row: ChatSessionRow) -> None:
        """在同一事务中解除物化绑定，并保留可重放的 Transcript 事件。"""

        sequence = int(session.scalar(select(func.coalesce(func.max(ChatSessionEventRow.sequence), 0)).where(
            ChatSessionEventRow.session_id == row.session_id
        )) or 0) + 1
        created_at = datetime.now(timezone.utc).isoformat()
        event_id = str(uuid4())
        session.add(ChatSessionEventRow(
            session_id=row.session_id,
            sequence=sequence,
            event_json={
                "schema_version": 2,
                "event_id": event_id,
                "session_id": row.session_id,
                "sequence": sequence,
                "event_type": "book_bound",
                "created_at": created_at,
                "payload": {"book_id": None},
            },
        ))
        row.book_id = None
        row.last_sequence = sequence
        row.updated_at = created_at
