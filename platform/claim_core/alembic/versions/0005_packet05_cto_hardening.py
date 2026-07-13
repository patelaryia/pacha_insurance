"""Harden Packet-05 concurrent idempotency.

Revision ID: 0005_packet05_cto_hardening
Revises: 0004_docintel_live_stages
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_packet05_cto_hardening"
down_revision: str | None = "0004_docintel_live_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add atomic stage ownership and consistency input uniqueness."""

    dialect = op.get_bind().dialect.name
    with op.batch_alter_table("document_stages") as batch_op:
        batch_op.drop_constraint("ck_document_stages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_document_stages_status",
            "status IN ('pending','running','succeeded','failed','skipped')",
        )
    with op.batch_alter_table("consistency_results") as batch_op:
        batch_op.add_column(sa.Column("input_fingerprint", sa.Text(), nullable=True))
    if dialect == "postgresql":
        op.execute(
            "UPDATE consistency_results SET input_fingerprint = "
            "COALESCE(evidence->>'_input_fingerprint', id)"
        )
    else:
        op.execute(
            "UPDATE consistency_results SET input_fingerprint = "
            "COALESCE(json_extract(evidence, '$._input_fingerprint'), id)"
        )
    with op.batch_alter_table("consistency_results") as batch_op:
        batch_op.alter_column("input_fingerprint", existing_type=sa.Text(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_consistency_results_input",
            ["claim_id", "check_id", "input_fingerprint"],
        )


def downgrade() -> None:
    """Remove Packet-05 concurrency hardening."""

    with op.batch_alter_table("consistency_results") as batch_op:
        batch_op.drop_constraint("uq_consistency_results_input", type_="unique")
        batch_op.drop_column("input_fingerprint")
    with op.batch_alter_table("document_stages") as batch_op:
        batch_op.drop_constraint("ck_document_stages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_document_stages_status",
            "status IN ('pending','succeeded','failed','skipped')",
        )
