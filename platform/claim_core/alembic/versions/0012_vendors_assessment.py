"""Add PRD-07 vendors and assessor-report checklist ownership.

Revision ID: 0012_vendors_assessment
Revises: 0011_chase_checklists
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_vendors_assessment"
down_revision: str | None = "0011_chase_checklists"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the vendor registry and widen checklist purpose safely."""

    json_value = (
        postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    )
    emails_value = (
        postgresql.ARRAY(sa.Text())
        if op.get_bind().dialect.name == "postgresql"
        else sa.JSON()
    )
    op.create_table(
        "vendors",
        sa.Column("id", sa.Text(), primary_key=True, comment="Stable pack/vendor id"),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("emails", emails_value, nullable=False),
        sa.Column("fee_schedule", json_value, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "kind IN ('assessor', 'garage', 'supplier', 'salvage_yard')",
            name="ck_vendors_kind",
        ),
        comment="Append-only-by-policy PRD-07 vendor registry; deactivate, never delete.",
    )
    with op.batch_alter_table("chase_checklists", recreate="always") as batch:
        batch.drop_constraint("ck_chase_checklists_purpose", type_="check")
        batch.add_column(
            sa.Column(
                "requester_party_id",
                sa.Text(),
                sa.ForeignKey(
                    "parties.id", name="fk_chase_checklists_requester_party_id"
                ),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_chase_checklists_purpose",
            "purpose IN ('claim_docs', 'surrender', 'assessor_report')",
        )


def downgrade() -> None:
    """Restore the PRD-06 checklist shape and drop the vendor registry."""

    with op.batch_alter_table("chase_checklists", recreate="always") as batch:
        batch.drop_constraint("ck_chase_checklists_purpose", type_="check")
        batch.drop_column("requester_party_id")
        batch.create_check_constraint(
            "ck_chase_checklists_purpose",
            "purpose IN ('claim_docs', 'surrender')",
        )
    op.drop_table("vendors")
