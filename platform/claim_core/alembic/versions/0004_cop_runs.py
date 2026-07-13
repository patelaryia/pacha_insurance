"""Add append-only COP rule and calculation execution records.

Revision ID: 0004_cop_runs
Revises: 0003_document_stages
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_cop_runs"
down_revision: str | None = "0003_document_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create reconstructable rule_runs and calc_runs tables."""

    dialect = op.get_bind().dialect.name
    json_value = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
    op.create_table(
        "rule_runs",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column("pack_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fired", sa.Boolean()),
        sa.Column("outcome", json_value),
        sa.Column("inputs_snapshot", json_value, nullable=False),
        sa.Column("missing_inputs", json_value, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        comment="Append-only COP rule evaluation history.",
    )
    op.create_table(
        "calc_runs",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("calc_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("inputs", json_value, nullable=False),
        sa.Column("output", json_value),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column("pack_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("missing_inputs", json_value, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        comment="Append-only COP calculation execution history.",
    )


def downgrade() -> None:
    """Remove COP execution records in dependency order."""

    op.drop_table("calc_runs")
    op.drop_table("rule_runs")
