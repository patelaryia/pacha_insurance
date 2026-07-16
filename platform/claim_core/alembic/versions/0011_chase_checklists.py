"""Add the binding PRD-06 chase checklist evidence tables.

Revision ID: 0011_chase_checklists
Revises: 0010_agent_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_chase_checklists"
down_revision: str | None = "0010_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two never-purged PRD-06 tables."""

    op.create_table(
        "chase_checklists",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('claim_docs', 'surrender')",
            name="ck_chase_checklists_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'complete', 'cancelled')",
            name="ck_chase_checklists_status",
        ),
        comment="Never-purged PRD-06 checklist evidence.",
    )
    op.create_table(
        "chase_items",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "checklist_id",
            sa.Text(),
            sa.ForeignKey("chase_checklists.id"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("physical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waived_by", sa.Text(), nullable=True),
        sa.Column("waiver_reason", sa.Text(), nullable=True),
        sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.Text(), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','requested','received','verified','rejected','waived')",
            name="ck_chase_items_state",
        ),
        comment="Never-purged per-item document-cycle evidence.",
    )


def downgrade() -> None:
    """Drop the PRD-06 tables in dependency order."""

    op.drop_table("chase_items")
    op.drop_table("chase_checklists")
