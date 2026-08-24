"""创建 StoryWeaver 当前完整 PostgreSQL Schema。

这是项目首次公开时的基线迁移。它不支持从早期开发版数据库逐版本升级；
新环境应从空数据库执行 ``alembic upgrade head``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260824_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_value = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "books",
        sa.Column("book_id", sa.String(length=80), primary_key=True),
        sa.Column("metadata_json", json_value, nullable=False),
        sa.Column("foundation_json", json_value, nullable=False),
        sa.Column("state_json", json_value, nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "story_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("state_json", json_value, nullable=False),
        sa.UniqueConstraint("book_id", "chapter_number", name="uq_snapshot_book_chapter"),
    )
    op.create_table(
        "chapters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("metadata_json", json_value, nullable=False),
        sa.Column("draft_json", json_value, nullable=False),
        sa.Column("original_draft_json", json_value, nullable=False),
        sa.Column("plan_json", json_value, nullable=False),
        sa.Column("delta_json", json_value, nullable=False),
        sa.Column("initial_review_json", json_value, nullable=True),
        sa.Column("final_review_json", json_value, nullable=True),
        sa.Column("context_trace_json", json_value, nullable=True),
        sa.Column("draft_history_json", json_value, nullable=False),
        sa.Column("review_history_json", json_value, nullable=False),
        sa.UniqueConstraint("book_id", "chapter_number", name="uq_chapter_book_number"),
    )

    op.create_table(
        "chapter_plan_proposals",
        sa.Column("proposal_id", sa.String(length=128), primary_key=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("base_chapter_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.Column("proposal_json", json_value, nullable=False),
    )
    op.create_index("ix_chapter_plan_proposals_book_id", "chapter_plan_proposals", ["book_id"])
    op.create_index("ix_plan_proposals_book_status_updated", "chapter_plan_proposals", ["book_id", "status", "updated_at"])
    op.create_index("ix_plan_proposals_book_chapter", "chapter_plan_proposals", ["book_id", "chapter_number"])
    op.create_table(
        "chapter_plan_proposal_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.String(length=128), sa.ForeignKey("chapter_plan_proposals.proposal_id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("proposal_json", json_value, nullable=False),
        sa.UniqueConstraint("proposal_id", "version", name="uq_proposal_version"),
    )
    op.create_table(
        "chapter_candidates",
        sa.Column("candidate_id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), sa.ForeignKey("chapter_plan_proposals.proposal_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revised", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", json_value, nullable=False),
        sa.Column("proposal_json", json_value, nullable=False),
        sa.Column("draft_json", json_value, nullable=False),
        sa.Column("final_draft_json", json_value, nullable=False),
        sa.Column("initial_review_json", json_value, nullable=False),
        sa.Column("final_review_json", json_value, nullable=False),
        sa.Column("context_trace_json", json_value, nullable=False),
        sa.Column("draft_history_json", json_value, nullable=False),
        sa.Column("review_history_json", json_value, nullable=False),
    )
    op.create_index("ix_chapter_candidates_book_id", "chapter_candidates", ["book_id"])
    op.create_index("ix_chapter_candidates_proposal_id", "chapter_candidates", ["proposal_id"])
    op.create_index("ix_candidates_book_chapter_created", "chapter_candidates", ["book_id", "chapter_number", "created_at"])
    op.create_index("ix_candidates_book_status_created", "chapter_candidates", ["book_id", "status", "created_at"])
    op.create_table(
        "chapter_rewrites",
        sa.Column("rewrite_id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=False),
        sa.Column("record_json", json_value, nullable=False),
        sa.Column("archive_json", json_value, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
    )
    op.create_index("ix_chapter_rewrites_book_id", "chapter_rewrites", ["book_id"])

    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=80), nullable=True),
        sa.Column("lock_scope", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload_json", json_value, nullable=False),
        sa.Column("result_json", json_value, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_jobs_book_id", "jobs", ["book_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index(
        "uq_active_job_per_book", "jobs", ["book_id"], unique=True,
        postgresql_where=sa.text("book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running')"),
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("payload_json", json_value, nullable=False),
        sa.UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])

    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("book_id", sa.String(length=80), nullable=True),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_chat_sessions_book_id", "chat_sessions", ["book_id"])
    op.create_index("ix_chat_sessions_updated_at", "chat_sessions", ["updated_at"])
    op.create_table(
        "chat_session_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_json", json_value, nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_chat_event_sequence"),
    )
    op.create_index("ix_chat_session_events_session_id", "chat_session_events", ["session_id"])
    op.create_table(
        "action_proposals",
        sa.Column("proposal_id", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=64), sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("book_id", sa.String(length=80), nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", json_value, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("confirmed_at", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_action_proposals_session_id", "action_proposals", ["session_id"])
    op.create_index("ix_action_proposals_book_id", "action_proposals", ["book_id"])
    op.create_index("ix_action_proposals_status", "action_proposals", ["status"])
    op.create_index("ix_action_proposals_session_status", "action_proposals", ["session_id", "status"])
    op.create_index("ix_action_proposals_book_status", "action_proposals", ["book_id", "status"])

    op.create_table(
        "creative_controls",
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("author_intent", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_focus", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_focus_mode", sa.String(length=24), nullable=False, server_default="persistent"),
        sa.Column("focus_target_chapter", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "long_term_memories",
        sa.Column("memory_id", sa.String(length=128), primary_key=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("record_json", json_value, nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", "fingerprint", "status", name="uq_memory_scope_fingerprint_status"),
    )
    op.create_index("ix_long_term_memories_scope_type", "long_term_memories", ["scope_type"])
    op.create_index("ix_long_term_memories_scope_id", "long_term_memories", ["scope_id"])
    op.create_index("ix_long_term_memories_status", "long_term_memories", ["status"])


def downgrade() -> None:
    # 反向删除按外键依赖的逆序执行。
    for table in (
        "long_term_memories", "creative_controls", "action_proposals",
        "chat_session_events", "chat_sessions", "job_events", "jobs",
        "chapter_rewrites", "chapter_candidates", "chapter_plan_proposal_versions",
        "chapter_plan_proposals", "chapters", "story_snapshots", "books",
    ):
        op.drop_table(table)
