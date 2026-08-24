"""PostgreSQL 表定义。

领域对象继续由现有严格序列化器负责编码；JSON 列保存各稳定聚合与不可变产物。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
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
    state_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class StorySnapshotRow(Base):
    __tablename__ = "story_snapshots"
    __table_args__ = (UniqueConstraint("book_id", "chapter_number", name="uq_snapshot_book_chapter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


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


class PlanProposalRow(Base):
    __tablename__ = "chapter_plan_proposals"
    __table_args__ = (
        Index("ix_plan_proposals_book_status_updated", "book_id", "status", "updated_at"),
        Index("ix_plan_proposals_book_chapter", "book_id", "chapter_number"),
    )

    proposal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


class PlanProposalVersionRow(Base):
    __tablename__ = "chapter_plan_proposal_versions"
    __table_args__ = (UniqueConstraint("proposal_id", "version", name="uq_proposal_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("chapter_plan_proposals.proposal_id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


class ChapterCandidateRow(Base):
    __tablename__ = "chapter_candidates"
    __table_args__ = (
        Index("ix_candidates_book_chapter_created", "book_id", "chapter_number", "created_at"),
        Index("ix_candidates_book_status_created", "book_id", "status", "created_at"),
    )

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("chapter_plan_proposals.proposal_id", ondelete="RESTRICT"), nullable=False, index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revised: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    draft_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    final_draft_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    initial_review_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    final_review_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    context_trace_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    draft_history_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    review_history_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)


class RewriteRow(Base):
    __tablename__ = "chapter_rewrites"

    rewrite_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False, index=True)
    record_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    archive_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "uq_active_job_per_book",
            "book_id",
            unique=True,
            postgresql_where=(
                text("book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running')")
            ),
            sqlite_where=(
                text("book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running')")
            ),
        ),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # 锁范围描述任务是否会改变作品正史；普通聊天不占用作品写锁。
    lock_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ActionProposalRow(Base):
    """自然语言产生、等待用户确认的写操作。"""

    __tablename__ = "action_proposals"
    __table_args__ = (
        Index("ix_action_proposals_session_status", "session_id", "status"),
        Index("ix_action_proposals_book_status", "book_id", "status"),
    )

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    book_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, unique=True
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class CreativeControlRow(Base):
    """作者意图与当前焦点的作品级控制面。"""

    __tablename__ = "creative_controls"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.book_id", ondelete="CASCADE"), primary_key=True
    )
    author_intent: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_focus: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # single_chapter 在目标章节原子提交后清空；persistent 仅由作者修改。
    current_focus_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="persistent")
    focus_target_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
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

    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    record_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "fingerprint", "status", name="uq_memory_scope_fingerprint_status"),)


class ContextSnapshotRow(Base):
    """一次 Agent 运行实际消费的冻结上下文与证据轨迹。"""

    __tablename__ = "context_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, index=True
    )
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.book_id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    book_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_context: Mapped[str] = mapped_column(Text, nullable=False)
    trace_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
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
