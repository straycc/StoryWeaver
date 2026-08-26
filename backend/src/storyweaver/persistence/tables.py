"""StoryWeaver 的收敛 PostgreSQL 表定义。"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .database import Base

JSON_VALUE = JSON().with_variant(JSONB, "postgresql")


class BookRow(Base):
    __tablename__ = "books"
    book_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    foundation_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    initial_state_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    state_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    creative_control_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ChapterRow(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "chapter_number", name="uq_chapter_book_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    draft_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    original_draft_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    plan_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    delta_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    initial_review_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    final_review_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    context_trace_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    draft_history_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    review_history_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    state_after_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


class ChapterRunRow(Base):
    """章节从计划、候选稿到重写归档的唯一过程聚合。"""
    __tablename__ = "chapter_runs"
    __table_args__ = (
        Index("ix_chapter_runs_book_chapter_updated", "book_id", "chapter_number", "updated_at"),
        Index("ix_chapter_runs_book_status_updated", "book_id", "status", "updated_at"),
    )
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    base_book_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    plan_history_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    artifacts_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("uq_active_job_per_book", "book_id", unique=True,
        postgresql_where=text("book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running')"),
        sqlite_where=text("book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running')")),)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    lock_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ActionProposalRow(Base):
    __tablename__ = "action_proposals"
    __table_args__ = (Index("ix_action_proposals_session_status", "session_id", "status"), Index("ix_action_proposals_book_status", "book_id", "status"))
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    book_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, unique=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class JobEventRow(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


class LongTermMemoryRow(Base):
    __tablename__ = "long_term_memories"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "fingerprint", "status", name="uq_memory_scope_fingerprint_status"),)
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    record_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


class ContextSnapshotRow(Base):
    __tablename__ = "context_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, index=True)
    simulation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    simulation_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    simulation_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    simulation_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    simulation_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    book_id: Mapped[str | None] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=True, index=True)
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    book_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_context: Mapped[str] = mapped_column(Text, nullable=False)
    trace_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class SimulationSessionRow(Base):
    __tablename__ = "simulation_sessions"
    simulation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False, index=True)
    base_book_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    user_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    current_turn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    base_snapshot_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    scene_config_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    current_state_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class SimulationTurnRow(Base):
    __tablename__ = "simulation_turns"
    __table_args__ = (UniqueConstraint("simulation_id", "turn_number", name="uq_simulation_turn_number"), UniqueConstraint("simulation_id", "client_request_id", name="uq_simulation_client_request"), Index("uq_simulation_active_turn", "simulation_id", unique=True, postgresql_where=text("status IN ('queued', 'running')"), sqlite_where=text("status IN ('queued', 'running')")))
    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulation_sessions.simulation_id", ondelete="CASCADE"), nullable=False, index=True)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, index=True)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    user_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_character_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    state_delta_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    state_before_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state_after_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("context_snapshots.snapshot_id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ChatSessionRow(Base):
    __tablename__ = "chat_sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ChatSessionEventRow(Base):
    __tablename__ = "chat_session_events"
    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_chat_event_sequence"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
