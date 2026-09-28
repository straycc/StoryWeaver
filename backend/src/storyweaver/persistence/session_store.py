"""SQLite 会话事件仓储。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import delete, func, select

from ..application.models import ChatMessage, ChatSession, ChatSessionSummary, TimelinePage, TranscriptEvent
from .database import Database
from .tables import ActionProposalRow, ChatSessionEventRow, ChatSessionRow, JobRow
from .timeline import TimelineProjector


class SQLAlchemyChatSessionRepository:
    """SQLite 会话事件仓储。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._timeline_projector = TimelineProjector()

    def create_session(self, *, title: str = "新对话", book_id: str | None = None) -> ChatSession:
        now = self._timestamp()
        session_id = str(uuid4())
        reused_session_id: str | None = None
        with self.database.session() as session:
            with self.database.write_transaction(session):
                normalized_title = title.strip() or "新对话"
                normalized_book_id = book_id.strip() if book_id and book_id.strip() else None
                if normalized_title == "新对话" and normalized_book_id is None:
                    for row in self._empty_candidates(session):
                        if self._is_reusable_empty(session, row):
                            reused_session_id = row.session_id
                            break
                if reused_session_id is None:
                    session.add(ChatSessionRow(
                        session_id=session_id,
                        created_at=now,
                        title=normalized_title,
                        book_id=normalized_book_id,
                        updated_at=now,
                        last_sequence=0,
                        message_count=0,
                    ))
                    session.flush()
                    self._append(session, session_id, "session_created", {
                        "title": normalized_title, "book_id": normalized_book_id,
                        "created_at": now, "updated_at": now,
                    }, created_at=now)
        return self.load_session(reused_session_id or session_id)

    def load_session(self, session_id: str) -> ChatSession:
        events = self.list_events(session_id)
        if not events:
            raise KeyError(f"会话不存在：{session_id}")
        return self._project(events)

    def list_sessions(self) -> tuple[ChatSessionSummary, ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(ChatSessionRow).order_by(ChatSessionRow.updated_at.desc())
            ).all()
            summaries: list[ChatSessionSummary] = []
            found_empty = False
            for row in rows:
                if self._is_reusable_empty(session, row):
                    if found_empty:
                        continue
                    found_empty = True
                summaries.append(ChatSessionSummary(
                    session_id=row.session_id,
                    title=row.title,
                    updated_at=row.updated_at,
                    book_id=row.book_id,
                    message_count=row.message_count,
                ))
        return tuple(summaries)

    def prune_duplicate_empty_sessions(self) -> int:
        """启动时清理多余的原始空对话，保留最新的一条。"""
        removed = 0
        with self.database.session() as session:
            with self.database.write_transaction(session):
                found_empty = False
                for row in self._empty_candidates(session):
                    if not self._is_reusable_empty(session, row):
                        continue
                    if not found_empty:
                        found_empty = True
                        continue
                    session.execute(delete(ChatSessionEventRow).where(
                        ChatSessionEventRow.session_id == row.session_id
                    ))
                    session.delete(row)
                    removed += 1
        return removed

    @staticmethod
    def _empty_candidates(session: Any) -> list[ChatSessionRow]:
        return session.scalars(select(ChatSessionRow).where(
            ChatSessionRow.book_id.is_(None),
            ChatSessionRow.title == "新对话",
            ChatSessionRow.message_count == 0,
        ).order_by(ChatSessionRow.updated_at.desc())).all()

    @staticmethod
    def _is_reusable_empty(session: Any, row: ChatSessionRow) -> bool:
        if row.book_id is not None or row.title != "新对话" or row.message_count != 0:
            return False
        events = session.scalars(select(ChatSessionEventRow).where(
            ChatSessionEventRow.session_id == row.session_id
        )).all()
        if any(event.event_json.get("event_type") not in {"session_created", "session_model_selected", "session_reasoning_selected"} for event in events):
            return False
        # 已关联任务或提案的会话即使没有消息，也不视作可复用草稿。
        if session.scalar(select(JobRow.job_id).where(
            JobRow.payload_json["session_id"].as_string() == row.session_id
        ).limit(1)) is not None:
            return False
        return session.scalar(select(ActionProposalRow.proposal_id).where(
            ActionProposalRow.session_id == row.session_id
        ).limit(1)) is None

    def append_message(self, session_id: str, *, role: str, content: str, action: str = "chat", metadata: Mapping[str, Any] | None = None) -> ChatSession:
        now = self._timestamp()
        with self.database.session() as session:
            with self.database.write_transaction(session):
                self._append(session, session_id, "message_added", {
                    "message_id": str(uuid4()), "role": role, "content": content,
                    "action": action, "metadata": dict(metadata or {}),
                }, created_at=now)
        return self.load_session(session_id)

    def bind_book(self, session_id: str, book_id: str | None, *, allow_nonempty: bool = False) -> ChatSession:
        current = self.load_session(session_id)
        normalized = book_id.strip() if book_id and book_id.strip() else None
        if current.book_id == normalized:
            return current
        if not allow_nonempty and self._has_activity(session_id):
            raise ValueError("当前会话已有消息或创作动作，不能原地切换作品；请为目标作品创建新会话")
        self.append_event(session_id, event_type="book_bound", payload={"book_id": normalized})
        return self.load_session(session_id)

    def current_binding_sequence(self, session_id: str) -> int:
        events = self.list_events(session_id)
        for event in reversed(events):
            if event.event_type in {"session_created", "book_bound", "story_timeline_rewritten"}:
                return event.sequence
        raise ValueError("会话缺少创建或绑定事件")

    def append_event(self, session_id: str, *, event_type: str, payload: Mapping[str, Any] | None = None) -> TranscriptEvent:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                return self._append(session, session_id, event_type, dict(payload or {}))

    def selected_model(self, session_id: str) -> tuple[str, str] | None:
        for event in reversed(self.list_events(session_id)):
            if event.event_type == "session_model_selected":
                return str(event.payload["provider_id"]), str(event.payload["model_id"])
        return None

    def selected_reasoning(self, session_id: str) -> str:
        for event in reversed(self.list_events(session_id)):
            if event.event_type == "session_reasoning_selected":
                return str(event.payload["level"])
        return "default"

    def list_events(self, session_id: str) -> tuple[TranscriptEvent, ...]:
        with self.database.session() as session:
            if session.get(ChatSessionRow, session_id) is None:
                raise KeyError(f"会话不存在：{session_id}")
            rows = session.scalars(select(ChatSessionEventRow).where(
                ChatSessionEventRow.session_id == session_id,
            ).order_by(ChatSessionEventRow.sequence)).all()
        return tuple(self._decode(row.event_json) for row in rows)

    def load_timeline(self, session_id: str, *, before_sequence: int | None = None, limit: int = 50) -> TimelinePage:
        if limit < 1 or limit > 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        events = self._timeline_projector.project(
            item for item in self.list_events(session_id)
            if before_sequence is None or item.sequence < before_sequence
        )
        page = events[-limit:]
        return TimelinePage(page, len(events) > len(page), page[0].sequence if len(events) > len(page) and page else None)

    def latest_summary(self, session_id: str) -> TranscriptEvent | None:
        summaries = [item for item in self.list_events(session_id) if item.event_type == "summary_updated"]
        return summaries[-1] if summaries else None

    def delete_session(self, session_id: str) -> None:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(ChatSessionRow, session_id)
                if row is None:
                    raise KeyError(f"会话不存在：{session_id}")
                session.delete(row)

    def _append(self, session: Any, session_id: str, event_type: str, payload: Mapping[str, Any], *, created_at: str | None = None) -> TranscriptEvent:
        # 同一会话可能同时收到浏览器请求与后台摘要/记忆事件；先锁住会话行，
        # 再读取最大 sequence，避免两个事务分配到同一个事件序号。
        row = session.get(ChatSessionRow, session_id)
        if row is None:
            raise KeyError(f"会话不存在：{session_id}")
        sequence = int(session.scalar(select(func.coalesce(func.max(ChatSessionEventRow.sequence), 0)).where(ChatSessionEventRow.session_id == session_id)) or 0) + 1
        timestamp = created_at or self._timestamp()
        event = TranscriptEvent(2, str(uuid4()), session_id, sequence, event_type, timestamp, payload)
        session.add(ChatSessionEventRow(session_id=session_id, sequence=sequence, event_json=self._encode(event)))
        row.updated_at = timestamp
        row.last_sequence = sequence
        if event_type == "book_bound":
            row.book_id = str(payload["book_id"]) if payload.get("book_id") else None
        elif event_type == "message_added":
            row.message_count += 1
            if row.title == "新对话" and payload.get("role") == "user":
                compact = " ".join(str(payload.get("content") or "").split())
                if compact:
                    row.title = compact[:28] + ("…" if len(compact) > 28 else "")
        return event

    def _has_activity(self, session_id: str) -> bool:
        return any(item.event_type not in {"session_created", "summary_updated", "memory_extracted"} for item in self.list_events(session_id))

    @staticmethod
    def _project(events: tuple[TranscriptEvent, ...]) -> ChatSession:
        created = events[0]
        if created.event_type != "session_created":
            raise ValueError("Transcript 第一条事件必须是 session_created")
        title = str(created.payload.get("title") or "新对话")
        book_id = str(created.payload["book_id"]) if created.payload.get("book_id") else None
        created_at = str(created.payload.get("created_at") or created.created_at)
        updated_at = str(created.payload.get("updated_at") or created.created_at)
        messages: list[ChatMessage] = []
        for event in events[1:]:
            updated_at = event.created_at
            if event.event_type == "book_bound":
                book_id = str(event.payload["book_id"]) if event.payload.get("book_id") else None
            elif event.event_type == "message_added":
                message = ChatMessage(
                    message_id=str(event.payload.get("message_id") or event.event_id), role=str(event.payload.get("role") or ""),
                    content=str(event.payload.get("content") or ""), created_at=event.created_at,
                    action=str(event.payload.get("action") or "chat"), metadata=event.payload.get("metadata") if isinstance(event.payload.get("metadata"), Mapping) else {}, sequence=event.sequence,
                )
                messages.append(message)
                if title == "新对话" and message.role == "user":
                    compact = " ".join(message.content.split())
                    title = compact[:28] + ("…" if len(compact) > 28 else "")
        return ChatSession(session_id=created.session_id, title=title, created_at=created_at, updated_at=updated_at, book_id=book_id, messages=tuple(messages))

    @staticmethod
    def _encode(event: TranscriptEvent) -> dict[str, Any]:
        return {"schema_version": event.schema_version, "event_id": event.event_id, "session_id": event.session_id, "sequence": event.sequence, "event_type": event.event_type, "created_at": event.created_at, "payload": dict(event.payload)}

    @staticmethod
    def _decode(data: Mapping[str, Any]) -> TranscriptEvent:
        return TranscriptEvent(**dict(data))

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
