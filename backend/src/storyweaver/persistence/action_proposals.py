"""ActionProposal 的持久化与状态投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select

from .database import Database
from .tables import ActionProposalRow


ACTION_PROPOSAL_STATUSES = frozenset({
    "pending", "confirmed", "cancelled", "completed", "failed", "expired",
})


@dataclass(frozen=True, slots=True)
class ActionProposal:
    proposal_id: str
    session_id: str
    book_id: str | None
    action_type: str
    payload: Mapping[str, Any]
    summary: str
    status: str
    job_id: str | None
    created_at: str
    confirmed_at: str | None
    expires_at: str | None
    updated_at: str


class ActionProposalRepository:
    """Proposal 只记录用户确认边界，执行细节始终属于 Job。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, session_id: str, book_id: str | None, action_type: str,
               payload: Mapping[str, Any], summary: str, expires_at: str | None = None) -> ActionProposal:
        now = self._now()
        row = ActionProposalRow(
            proposal_id=str(uuid4()), session_id=session_id, book_id=book_id,
            action_type=action_type, payload_json=dict(payload), summary=summary,
            status="pending", job_id=None, created_at=now, confirmed_at=None,
            expires_at=expires_at, updated_at=now,
        )
        with self.database.session() as session:
            with session.begin():
                session.add(row)
        return self.get(row.proposal_id)

    def get(self, proposal_id: str) -> ActionProposal:
        with self.database.session() as session:
            row = session.get(ActionProposalRow, proposal_id)
        if row is None:
            raise KeyError(f"ActionProposal 不存在：{proposal_id}")
        return self._decode(row)

    def list_pending(self, *, session_id: str) -> tuple[ActionProposal, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(ActionProposalRow).where(
                ActionProposalRow.session_id == session_id,
                ActionProposalRow.status == "pending",
            ).order_by(ActionProposalRow.created_at.desc())).all()
        return tuple(self._decode(row) for row in rows)

    def expire_for_chapter_plan(self, *, book_id: str, chapter_plan_proposal_id: str) -> tuple[ActionProposal, ...]:
        """候选计划变更后，使引用旧计划的确认写作请求立即失效。"""

        pending = self._pending_for_book(book_id)
        expired: list[ActionProposal] = []
        for proposal in pending:
            if proposal.action_type != "confirm_and_write_chapter":
                continue
            if str(proposal.payload.get("proposal_id") or "") == chapter_plan_proposal_id:
                expired.append(self.expire(proposal.proposal_id))
        return tuple(expired)

    def confirm(self, proposal_id: str, *, job_id: str) -> ActionProposal:
        return self._transition(proposal_id, "confirmed", job_id=job_id)

    def cancel(self, proposal_id: str) -> ActionProposal:
        return self._transition(proposal_id, "cancelled")

    def expire(self, proposal_id: str) -> ActionProposal:
        return self._transition(proposal_id, "expired")

    def project_job_terminal(self, proposal_id: str, *, succeeded: bool) -> ActionProposal:
        return self._transition(proposal_id, "completed" if succeeded else "failed", allow_from={"confirmed"})

    def _transition(self, proposal_id: str, status: str, *, job_id: str | None = None,
                    allow_from: set[str] | None = None) -> ActionProposal:
        if status not in ACTION_PROPOSAL_STATUSES:
            raise ValueError(f"不支持的 ActionProposal 状态：{status}")
        with self.database.session() as session:
            with session.begin():
                row = session.get(ActionProposalRow, proposal_id, with_for_update=True)
                if row is None:
                    raise KeyError(f"ActionProposal 不存在：{proposal_id}")
                accepted = allow_from or {"pending"}
                if row.status not in accepted:
                    raise ValueError(f"ActionProposal 当前为 {row.status}，不能变更为 {status}")
                row.status = status
                row.updated_at = self._now()
                if status == "confirmed":
                    row.confirmed_at = row.updated_at
                    row.job_id = job_id
                session.flush()
                return self._decode(row)

    def _pending_for_book(self, book_id: str) -> tuple[ActionProposal, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(ActionProposalRow).where(
                ActionProposalRow.book_id == book_id, ActionProposalRow.status == "pending",
            )).all()
        return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _decode(row: ActionProposalRow) -> ActionProposal:
        return ActionProposal(
            proposal_id=row.proposal_id, session_id=row.session_id, book_id=row.book_id,
            action_type=row.action_type, payload=dict(row.payload_json), summary=row.summary,
            status=row.status, job_id=row.job_id, created_at=row.created_at,
            confirmed_at=row.confirmed_at, expires_at=row.expires_at, updated_at=row.updated_at,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
