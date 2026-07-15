"""Add the PACKET-12 staff notifications projection.

Revision ID: 0009_notifications
Revises: 0008_review_items
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_notifications"
down_revision: str | None = "0008_review_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the locally specified, rebuildable notification projection."""

    json_value = (
        postgresql.JSONB()
        if op.get_bind().dialect.name == "postgresql"
        else sa.JSON()
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "recipient",
            sa.Text(),
            nullable=False,
            comment="staff actor, user:<ULID>",
        ),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column(
            "event_id",
            sa.Text(),
            nullable=False,
            comment="source event id or deterministic digest key",
        ),
        sa.Column("claim_id", sa.Text(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", json_value, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('in_app', 'email')", name="ck_notifications_channel"
        ),
        sa.CheckConstraint(
            "status IN ('sent', 'staged', 'read')", name="ck_notifications_status"
        ),
        sa.UniqueConstraint(
            "recipient",
            "event_id",
            "channel",
            name="uq_notifications_recipient_event_channel",
        ),
        comment="Rebuildable projection of staff notifications; read status is mutable.",
    )


def downgrade() -> None:
    """Drop the PACKET-12 notification projection."""

    op.drop_table("notifications")
