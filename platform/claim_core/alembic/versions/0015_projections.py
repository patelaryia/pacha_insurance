"""Add the PRD-09 §9.2 projections table.

Revision ID: 0015_projections
Revises: 0014_note_drafts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_projections"
down_revision: str | None = "0014_note_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the binding PRD-09 §9.2 table with no invented columns."""

    json_value = (
        postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    )
    op.create_table(
        "projections",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, comment="paste_assist|rpa|api"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="queued|executing|verifying|completed|failed|diverged",
        ),
        sa.Column(
            "payload",
            json_value,
            nullable=False,
            comment="field paths + values + versions (snapshot)",
        ),
        sa.Column("readback", json_value, nullable=True),
        sa.Column("divergence", json_value, nullable=True),
        sa.Column(
            "evidence",
            json_value,
            nullable=True,
            comment="screenshot s3 keys per step (rpa), confirm ts + user (paste_assist)",
        ),
        sa.Column("attempts", sa.Integer(), nullable=True, server_default=sa.text("0")),
        sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        comment="PRD-09 §9.2 projections; payload snapshots are never mutated.",
    )


def downgrade() -> None:
    """Drop the PRD-09 projections table."""

    op.drop_table("projections")
