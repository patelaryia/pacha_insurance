"""Add PACKET-24 durable Graph outbound release state.

Revision ID: 0018_graph_outbound
Revises: 0017_graph_inbound
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_graph_outbound"
down_revision: str | None = "0017_graph_inbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_outbound_releases",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("write_id", sa.Text(), nullable=False, unique=True),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("action_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("release_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upload_session", sa.Text(), nullable=True),
        sa.Column("upload_attachment_id", sa.Text(), nullable=True),
        sa.Column("upload_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("graph_message_id", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_graph_outbound_release_due",
        "graph_outbound_releases",
        ["status", "release_due_at"],
    )
    op.create_table(
        "graph_outbound_rate_buckets",
        sa.Column("bucket_id", sa.Text(), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("graph_outbound_rate_buckets")
    op.drop_index("ix_graph_outbound_release_due", table_name="graph_outbound_releases")
    op.drop_table("graph_outbound_releases")
