"""Add PRD-03 eval harness and autonomy controller tables.

Revision ID: 0007_eval_autonomy
Revises: 0006_packet05_cto_hardening
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_eval_autonomy"
down_revision: str | None = "0006_packet05_cto_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the four binding PRD-03 section 3.2 tables."""

    dialect = op.get_bind().dialect.name
    json_value = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
    tags_type = postgresql.ARRAY(sa.Text()) if dialect == "postgresql" else sa.JSON()
    op.create_table(
        "test_cases",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("corpus", sa.Text(), nullable=False, comment="'motor_v1'"),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="'seed_closed_claim'|'production_correction'",
        ),
        sa.Column(
            "input_bundle",
            json_value,
            nullable=False,
            comment="S3 refs to docs/emails (anonymised for seed)",
        ),
        sa.Column(
            "expected",
            json_value,
            nullable=False,
            comment=(
                "{fields:{path:value}, rules:{id:fired}, calcs:{id:output}, "
                "note_rubric:{...}}"
            ),
        ),
        sa.Column("tags", tags_type),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "grader_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("grader_id", sa.Text()),
        sa.Column(
            "subject_type", sa.Text(), comment="'field'|'rule'|'calc'|'artifact'"
        ),
        sa.Column("subject_ref", json_value),
        sa.Column("claim_id", sa.Text(), comment="one of claim/test populated"),
        sa.Column("test_case_id", sa.Text()),
        sa.Column("result", sa.Text(), nullable=False, comment="'pass'|'fail'|'error'"),
        sa.Column(
            "severity", sa.Text(), nullable=False, comment="'critical'|'major'|'minor'"
        ),
        sa.Column("detail", json_value),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "capabilities",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            comment="e.g. 'intake.acknowledge','pack.note_draft'",
        ),
        sa.Column(
            "current_level",
            sa.Text(),
            nullable=False,
            server_default="L1",
            comment="L0..L4",
        ),
        sa.Column(
            "max_level",
            sa.Text(),
            nullable=False,
            comment="hard ceiling, e.g. consistency checks = 'L2'",
        ),
        sa.Column(
            "policy",
            json_value,
            nullable=False,
            comment="promotion policy, 3.4",
        ),
    )
    op.create_table(
        "autonomy_changes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("capability_id", sa.Text()),
        sa.Column("from_level", sa.Text()),
        sa.Column("to_level", sa.Text()),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            comment="'promotion'|'auto_demotion'|'manual'",
        ),
        sa.Column(
            "evidence",
            json_value,
            nullable=False,
            comment="run counts, pass rates, window",
        ),
        sa.Column("approved_by", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Remove PRD-03 tables in dependency order."""

    op.drop_table("autonomy_changes")
    op.drop_table("capabilities")
    op.drop_table("grader_runs")
    op.drop_table("test_cases")
