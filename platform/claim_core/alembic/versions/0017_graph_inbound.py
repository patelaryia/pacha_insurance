"""Add PACKET-23 Graph inbound binding and receipt state.

Revision ID: 0017_graph_inbound
Revises: 0016_temporal_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_graph_inbound"
down_revision: str | None = "0016_temporal_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_bindings",
        sa.Column("mailbox_ref", sa.Text(), primary_key=True),
        sa.Column("delta_token", sa.Text(), nullable=True),
        sa.Column("subscription_id", sa.Text(), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_state_sha256", sa.Text(), nullable=True),
        sa.Column("last_successful_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "graph_inbound_receipts",
        sa.Column("graph_message_id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
    )


def downgrade() -> None:
    op.drop_table("graph_inbound_receipts")
    op.drop_table("graph_bindings")
