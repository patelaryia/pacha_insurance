"""Add the PRD-08 §8.5 approval-note draft table.

Revision ID: 0014_note_drafts
Revises: 0013_savings_ledger
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_note_drafts"
down_revision: str | None = "0013_savings_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the binding PRD-08 §8.5 table with no invented columns."""

    json_value = (
        postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    )
    op.create_table(
        "note_drafts",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", json_value, nullable=False, comment="sections[] etc"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("edited_by", sa.Text(), nullable=True),
        sa.Column("signed_by", sa.Text(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("claim_id", "version", name="uq_note_drafts_claim_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'in_review', 'signed', 'superseded')",
            name="ck_note_drafts_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_note_drafts_version"),
        comment="Append-only PRD-08 approval-note draft versions.",
    )


def downgrade() -> None:
    """Drop the PRD-08 approval-note draft table."""

    op.drop_table("note_drafts")
