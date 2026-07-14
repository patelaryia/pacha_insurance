"""Add the PRD-04 rebuildable review queue projection.

Revision ID: 0008_review_items
Revises: 0007_eval_autonomy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from cop_runtime.contracts import REVIEW_ITEM_TYPES

revision: str = "0008_review_items"
down_revision: str | None = "0007_eval_autonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quoted(values: frozenset[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def upgrade() -> None:
    """Create the locally specified PACKET-10 projection table."""

    json_value = postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    op.create_table(
        "review_items",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("subtype", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", json_value, nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_payload", json_value, nullable=True),
        sa.Column("resolution_schema_version", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"type IN ({_quoted(REVIEW_ITEM_TYPES)})", name="ck_review_items_type"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'cancelled')", name="ck_review_items_status"
        ),
        sa.UniqueConstraint("source_event_id", name="uq_review_items_source_event_id"),
        comment="Rebuildable projection of review.created events (PRD-04).",
    )


def downgrade() -> None:
    """Drop the rebuildable review projection."""

    op.drop_table("review_items")
