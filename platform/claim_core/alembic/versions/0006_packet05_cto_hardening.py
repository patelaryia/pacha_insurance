"""Harden Packet-05 concurrent idempotency.

Revision ID: 0006_packet05_cto_hardening
Revises: 0005_docintel_live_stages
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_packet05_cto_hardening"
down_revision: str | None = "0005_docintel_live_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add atomic stage ownership and consistency input uniqueness."""

    with op.batch_alter_table("document_stages") as batch_op:
        batch_op.drop_constraint("ck_document_stages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_document_stages_status",
            "status IN ('pending','running','succeeded','failed','paused','skipped')",
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_consistency_results_input ON consistency_results "
            "(claim_id, check_id, (CAST(evidence->>'_input_fingerprint' AS VARCHAR)))"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_consistency_results_input ON consistency_results "
            "(claim_id, check_id, JSON_EXTRACT(evidence, '$.\"_input_fingerprint\"'))"
        )


def downgrade() -> None:
    """Remove Packet-05 concurrency hardening."""

    op.drop_index("uq_consistency_results_input", table_name="consistency_results")
    op.execute(
        "UPDATE document_stages SET status = 'failed', "
        "last_error = CASE "
        "WHEN last_error IS NULL OR last_error = '' "
        "THEN 'downgraded from running/paused; manual recovery required' "
        "ELSE last_error || '; downgraded from running/paused; manual recovery required' "
        "END WHERE status IN ('running', 'paused')"
    )
    with op.batch_alter_table("document_stages") as batch_op:
        batch_op.drop_constraint("ck_document_stages_status", type_="check")
        batch_op.create_check_constraint(
            "ck_document_stages_status",
            "status IN ('pending','succeeded','failed','skipped')",
        )
