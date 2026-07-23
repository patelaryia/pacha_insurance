"""Add the append-only PRD-07 savings ledger.

Revision ID: 0013_savings_ledger
Revises: 0012_vendors_assessment
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_savings_ledger"
down_revision: str | None = "0012_vendors_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the never-purged ledger with a stored generated saving."""

    json_value = (
        postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    )
    op.create_table(
        "savings_ledger",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("baseline_amount", sa.BigInteger(), nullable=False),
        sa.Column("achieved_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "saving",
            sa.BigInteger(),
            sa.Computed("baseline_amount - achieved_amount", persisted=True),
        ),
        sa.Column("evidence", json_value, nullable=False),
        sa.Column("vendor_id", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('assessment_negotiation', 'supplier_substitution', "
            "'salvage_recovery')",
            name="ck_savings_ledger_kind",
        ),
        comment="Append-only PRD-07 savings ledger; rows are never deleted.",
    )


def downgrade() -> None:
    """Drop the PRD-07 savings ledger."""

    op.drop_table("savings_ledger")
