"""Add PRD-01 live-stage storage.

Revision ID: 0005_docintel_live_stages
Revises: 0004_cop_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_docintel_live_stages"
down_revision: str | None = "0004_cop_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add split linkage and append-only consistency/SLO tables."""

    dialect = op.get_bind().dialect.name
    json_value = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "parent_document_id",
                sa.Text(),
                nullable=True,
                comment="Immutable parent bundle for a human-resolved document split",
            )
        )
        batch_op.create_foreign_key(
            "fk_documents_parent_document_id",
            "documents",
            ["parent_document_id"],
            ["id"],
        )
    op.create_table(
        "consistency_results",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("check_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(4, 3)),
        sa.Column("evidence", json_value, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "doc_intel_samples",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "document_id", sa.Text(), sa.ForeignKey("documents.id"), nullable=False
        ),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(8, 6), nullable=False),
        sa.Column("breached_duration", sa.Boolean(), nullable=False),
        sa.Column("breached_cost", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Remove PRD-01 live-stage storage."""

    op.drop_table("doc_intel_samples")
    op.drop_table("consistency_results")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint(
            "fk_documents_parent_document_id", type_="foreignkey"
        )
        batch_op.drop_column("parent_document_id")
