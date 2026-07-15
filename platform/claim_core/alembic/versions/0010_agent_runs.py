"""Add the binding AR-1 agent_runs table.

Revision ID: 0010_agent_runs
Revises: 0009_notifications
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_agent_runs"
down_revision: str | None = "0009_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sole new PACKET-13 table, matching AR-1 verbatim."""

    json_value = postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID correlation id"),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("capability_id", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.Text(), nullable=True),
        sa.Column("trigger_event", sa.Text(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("steps", json_value, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("autonomy_level", sa.Text(), nullable=False),
        sa.Column("error", json_value, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'awaiting_review', 'completed', 'failed', 'blocked')",
            name="ck_agent_runs_status",
        ),
        comment="Durable AR-1 agent execution record.",
    )


def downgrade() -> None:
    """Drop the AR-1 table."""

    op.drop_table("agent_runs")
