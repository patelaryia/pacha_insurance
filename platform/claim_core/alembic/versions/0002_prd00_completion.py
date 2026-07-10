"""Complete PRD-00 audit, SLA, and platform-state storage.

Revision ID: 0002_prd00_completion
Revises: 0001_claim_substrate
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_prd00_completion"
down_revision: str | None = "0001_claim_substrate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the Packet-03 tables."""

    dialect = op.get_bind().dialect.name
    json_value = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
    sequence_type: sa.types.TypeEngine = (
        sa.BigInteger() if dialect == "postgresql" else sa.Integer()
    )

    op.create_table(
        "audit_ledger",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("seq", sequence_type, nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.Text()),
        sa.Column("object_ref", sa.Text()),
        sa.Column("before_hash", sa.Text()),
        sa.Column("after_hash", sa.Text()),
        sa.Column("detail", json_value, nullable=False),
        sa.Column("row_hash", sa.Text(), nullable=False),
        comment="Append-only, never-purged audit hash chain.",
    )
    op.create_table(
        "sla_definitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_event", sa.Text(), nullable=False),
        sa.Column("stop_event", sa.Text()),
        sa.Column("warn_after", sa.Text()),
        sa.Column("breach_after", sa.Text()),
        sa.Column("escalate_to_role", sa.Text(), nullable=False),
        sa.Column("calendar", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
    )
    op.create_table(
        "sla_clocks",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("definition_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("warn_at", sa.DateTime(timezone=True)),
        sa.Column("breach_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_by_event", sa.Text(), nullable=False),
        sa.Column("stopped_by_event", sa.Text()),
        comment="Outcome-pricing baseline; rows are never deleted.",
    )
    op.create_table(
        "platform_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", json_value, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Remove Packet-03 storage in dependency order."""

    op.drop_table("platform_state")
    op.drop_table("sla_clocks")
    op.drop_table("sla_definitions")
    op.drop_table("audit_ledger")
