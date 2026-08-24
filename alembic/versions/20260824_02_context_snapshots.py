"""持久化 Context V2 Snapshot。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260824_02"
down_revision = "20260824_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_value = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "context_snapshots",
        sa.Column("snapshot_id", sa.String(length=64), primary_key=True),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True),
        sa.Column("book_id", sa.String(length=80), sa.ForeignKey("books.book_id", ondelete="CASCADE"), nullable=True),
        sa.Column("agent_role", sa.String(length=64), nullable=False),
        sa.Column("book_version", sa.BigInteger(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("renderer_version", sa.String(length=64), nullable=False),
        sa.Column("rendered_context", sa.Text(), nullable=False),
        sa.Column("trace_json", json_value, nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_context_snapshots_job_id", "context_snapshots", ["job_id"])
    op.create_index("ix_context_snapshots_book_id", "context_snapshots", ["book_id"])
    op.create_index("ix_context_snapshots_agent_role", "context_snapshots", ["agent_role"])


def downgrade() -> None:
    op.drop_table("context_snapshots")
