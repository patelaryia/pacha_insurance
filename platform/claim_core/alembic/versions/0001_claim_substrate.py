"""Create the Packet-1 claim substrate.

Revision ID: 0001_claim_substrate
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_claim_substrate"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create every table in binding PRD-00 sections 0.2 and 0.3."""

    dialect = op.get_bind().dialect.name
    json_value = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
    binary_value = postgresql.BYTEA() if dialect == "postgresql" else sa.LargeBinary()

    op.create_table(
        "claims",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column("lob", sa.Text(), nullable=False, comment="LOB pack id"),
        sa.Column(
            "pack_version",
            sa.Text(),
            nullable=False,
            comment="Pack version pinned at creation",
        ),
        sa.Column("status", sa.Text(), nullable=False, comment="Claim FSM state"),
        sa.Column("substatus", sa.Text(), comment="Claim FSM substatus"),
        sa.Column(
            "external_refs",
            json_value,
            nullable=False,
            server_default=sa.text("'{}'"),
            comment="Denormalised read cache only",
        ),
        sa.Column(
            "dek_wrapped",
            binary_value,
            comment="Per-claim KMS-wrapped data-encryption key",
        ),
        sa.Column("assigned_to", sa.Text(), comment="Owning officer user id"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "claim_fields",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID"),
        sa.Column(
            "claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.Column("path", sa.Text(), nullable=False, comment="Dot-notation field path"),
        sa.Column("value", json_value, nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", json_value),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("verification_state", sa.Text(), nullable=False),
        sa.Column(
            "pii_class", sa.Text(), nullable=False, server_default=sa.text("'none'")
        ),
        sa.Column("value_search", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("superseded_by", sa.Text(), sa.ForeignKey("claim_fields.id")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("claim_id", "path", "version"),
    )
    op.create_index(
        "ix_fields_current",
        "claim_fields",
        ["claim_id", "path"],
        unique=False,
        postgresql_where=sa.text("superseded_by IS NULL"),
        sqlite_where=sa.text("superseded_by IS NULL"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.Column("doc_type", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False, comment="Immutable original"),
        sa.Column(
            "sha256",
            sa.Text(),
            nullable=False,
            comment="Dedupe and tamper evidence",
        ),
        sa.Column("page_count", sa.Integer()),
        sa.Column("source", json_value, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "communications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("claim_id", sa.Text(), sa.ForeignKey("claims.id")),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column(
            "channel", sa.Text(), nullable=False, server_default=sa.text("'email'")
        ),
        sa.Column("graph_message_id", sa.Text(), unique=True),
        sa.Column("thread_id", sa.Text()),
        sa.Column("from_addr", sa.Text()),
        sa.Column("to_addrs", json_value),
        sa.Column("subject", sa.Text()),
        sa.Column("body_s3_key", sa.Text(), nullable=False),
        sa.Column("sent_by", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "parties",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "claim_id", sa.Text(), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("meta", json_value, server_default=sa.text("'{}'")),
    )

    event_seq_default = None
    event_seq_type: sa.types.TypeEngine = sa.Integer()
    if dialect == "postgresql":
        op.execute(sa.schema.CreateSequence(sa.Sequence("events_seq_seq")))
        event_seq_type = sa.BigInteger()
        event_seq_default = sa.text("nextval('events_seq_seq')")

    op.create_table(
        "events",
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID identity"),
        sa.Column(
            "seq", event_seq_type, nullable=False, server_default=event_seq_default
        ),
        sa.Column("claim_id", sa.Text()),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", json_value, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "event_deliveries",
        sa.Column("event_id", sa.Text(), sa.ForeignKey("events.id"), primary_key=True),
        sa.Column("consumer", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0")),
        sa.Column("last_error", sa.Text()),
    )


def downgrade() -> None:
    """Remove the Packet-1 claim substrate."""

    dialect = op.get_bind().dialect.name
    op.drop_table("event_deliveries")
    op.drop_table("events")
    if dialect == "postgresql":
        op.execute(sa.schema.DropSequence(sa.Sequence("events_seq_seq")))
    op.drop_table("parties")
    op.drop_table("communications")
    op.drop_table("documents")
    op.drop_index("ix_fields_current", table_name="claim_fields")
    op.drop_table("claim_fields")
    op.drop_table("claims")
