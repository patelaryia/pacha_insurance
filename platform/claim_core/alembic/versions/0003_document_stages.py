"""Add durable PRD-01 document pipeline stage state.

Revision ID: 0003_document_stages
Revises: 0002_prd00_completion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_document_stages"
down_revision: str | None = "0002_prd00_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the resumable per-document stage table."""

    op.create_table(
        "document_stages",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "document_id", sa.Text(), sa.ForeignKey("documents.id"), nullable=False
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("output_ref", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('NORMALIZE','CLASSIFY','SPLIT','EXTRACT','CITE','VALIDATE',"
            "'COMMIT','CONSISTENCY')",
            name="ck_document_stages_stage",
        ),
        sa.CheckConstraint(
            "status IN ('pending','succeeded','failed','skipped')",
            name="ck_document_stages_status",
        ),
        sa.UniqueConstraint(
            "document_id", "stage", name="uq_document_stages_document_stage"
        ),
    )


def downgrade() -> None:
    """Remove the document stage table."""

    op.drop_table("document_stages")
