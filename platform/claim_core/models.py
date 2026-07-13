"""SQLAlchemy models for the binding PRD-00 section 0.2/0.3 schema."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Sequence,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from claim_core.types import JSON_VALUE


class Base(DeclarativeBase):
    """Declarative base for the claim-core package."""


class Claim(Base):
    """The deliberately thin root of a claim."""

    __tablename__ = "claims"
    __table_args__ = {
        "comment": "The claim root. Thin on purpose: real data lives in claim_fields."
    }

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    lob: Mapped[str] = mapped_column(
        Text, nullable=False, comment="'motor' | future pack ids"
    )
    pack_version: Mapped[str] = mapped_column(
        Text, nullable=False, comment="pinned at creation, e.g. 'motor@1.3.0'"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, comment="FSM state, see 0.4")
    substatus: Mapped[str | None] = mapped_column(
        Text, comment="e.g. 'EX_GRATIA_REVIEW' under DECLINED (see 0.4)"
    )
    external_refs: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
        comment="DENORMALISED READ CACHE ONLY — see note below",
    )
    dek_wrapped: Mapped[bytes | None] = mapped_column(
        LargeBinary, comment="per-claim data-encryption key, KMS-wrapped (ED-6a)"
    )
    assigned_to: Mapped[str | None] = mapped_column(
        Text, comment="owning officer user id (assignment model, PRD-05 §5.8)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimField(Base):
    """One immutable value version in the append-only field store."""

    __tablename__ = "claim_fields"
    __table_args__ = (
        UniqueConstraint("claim_id", "path", "version"),
        Index(
            "ix_fields_current",
            "claim_id",
            "path",
            postgresql_where=text("superseded_by IS NULL"),
            sqlite_where=text("superseded_by IS NULL"),
        ),
        {"comment": "Field store: append-only versions."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "dot notation: 'vehicle.reg', 'loss.date', "
            "'assessment.agreed_quote', 'reserve.total'"
        ),
    )
    value: Mapped[Any] = mapped_column(
        JSON_VALUE, nullable=False, comment="typed per field dictionary"
    )
    value_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'string'|'money'|'date'|'datetime'|'bool'|'enum'|'object'",
    )
    source_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "'extraction'|'calc'|'rule'|'human'|'system'|'projection_readback'"
        ),
    )
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_VALUE,
        comment=(
            "extraction: {document_id, page, bbox:[x0,y0,x1,y1], anchor_text}; "
            "calc: {calc_id, calc_run_id}; human: {user_id, review_item_id}"
        ),
    )
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), comment="null for human/system sources"
    )
    verification_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'extracted'|'human_verified'|'system_confirmed'",
    )
    pii_class: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="none",
        server_default=text("'none'"),
        comment="'none'|'personal-low'|'personal'|'sensitive'",
    )
    value_search: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "blind index: HMAC-SHA256(normalised value) under the KMS index key; "
            "populated only for national ID, KRA PIN, DL number, phone, bank account"
        ),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(ForeignKey("claim_fields.id"))
    created_by: Mapped[str] = mapped_column(
        Text, nullable=False, comment="'agent:intake'|'user:<ulid>'|'system'"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Document(Base):
    """Immutable original document metadata."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    parent_document_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("documents.id"),
        comment="Immutable parent bundle for a human-resolved document split",
    )
    doc_type: Mapped[str | None] = mapped_column(
        Text, comment="from pack taxonomy; null until classified"
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'received'|'classified'|'extracted'|'verified'|'rejected'",
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False, comment="Immutable original")
    sha256: Mapped[str] = mapped_column(Text, nullable=False, comment="Dedupe and tamper evidence")
    page_count: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment='{"channel":"email","message_id":"...","sender":"..."}',
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConsistencyResult(Base):
    """One immutable cross-document consistency evaluation."""

    __tablename__ = "consistency_results"
    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "check_id",
            "input_fingerprint",
            name="uq_consistency_results_input",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocIntelSample(Base):
    """One immutable per-document duration and cost sample."""

    __tablename__ = "doc_intel_samples"

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.id"), nullable=False
    )
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    breached_duration: Mapped[bool] = mapped_column(Boolean, nullable=False)
    breached_cost: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Communication(Base):
    """Inbound or outbound claim communication metadata."""

    __tablename__ = "communications"
    __table_args__ = {"comment": "every email in/out, later SMS/voice"}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[str | None] = mapped_column(Text, ForeignKey("claims.id"))
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, default="email", server_default=text("'email'")
    )
    graph_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    thread_id: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str | None] = mapped_column(Text)
    to_addrs: Mapped[list[str] | None] = mapped_column(JSON_VALUE)
    subject: Mapped[str | None] = mapped_column(Text)
    body_s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    sent_by: Mapped[str | None] = mapped_column(
        Text, comment="'agent:chase'|'user:<id>'"
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Party(Base):
    """A party related to a claim."""

    __tablename__ = "parties"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "'insured'|'broker'|'agent'|'driver'|'garage'|'assessor'|'supplier'|"
            "'third_party'|'bank'|'salvage_yard'"
        ),
    )
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_VALUE, default=dict, server_default=text("'{}'")
    )


event_seq = Sequence("events_seq_seq")


class Event(Base):
    """Transactional-outbox event row."""

    __tablename__ = "events"
    __table_args__ = {"comment": "Transactional outbox event spine."}

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID identity")
    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        event_seq,
        nullable=False,
        comment="transport order for external replay ONLY (see below)",
    )
    claim_id: Mapped[str | None] = mapped_column(
        Text, comment="nullable for platform events"
    )
    type: Mapped[str] = mapped_column(Text, nullable=False, comment="catalog below")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(
        Text, comment="ties multi-step agent runs together"
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventDelivery(Base):
    """Idempotent delivery state for a future outbox dispatcher."""

    __tablename__ = "event_deliveries"
    __table_args__ = {"comment": "at-least-once + idempotent consumers"}

    event_id: Mapped[str] = mapped_column(Text, ForeignKey("events.id"), primary_key=True)
    consumer: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int | None] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditLedgerRow(Base):
    """One immutable row in the single-writer audit hash chain."""

    __tablename__ = "audit_ledger"
    __table_args__ = {"comment": "Append-only, never-purged audit hash chain."}

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False, unique=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(Text)
    object_ref: Mapped[str | None] = mapped_column(Text)
    before_hash: Mapped[str | None] = mapped_column(Text)
    after_hash: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    row_hash: Mapped[str] = mapped_column(Text, nullable=False)


class SlaClock(Base):
    """A never-purged SLA outcome clock."""

    __tablename__ = "sla_clocks"
    __table_args__ = {"comment": "Outcome-pricing baseline; rows are never deleted."}

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    definition_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    breach_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="running|warned|breached|stopped",
    )
    started_by_event: Mapped[str] = mapped_column(Text, nullable=False)
    stopped_by_event: Mapped[str | None] = mapped_column(Text)


class SlaDefinitionRow(Base):
    """Persisted copy of the active, data-defined SLA registry."""

    __tablename__ = "sla_definitions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_event: Mapped[str] = mapped_column(Text, nullable=False)
    stop_event: Mapped[str | None] = mapped_column(Text)
    warn_after: Mapped[str | None] = mapped_column(Text)
    breach_after: Mapped[str | None] = mapped_column(Text)
    escalate_to_role: Mapped[str] = mapped_column(Text, nullable=False)
    calendar: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)


class PlatformState(Base):
    """Small durable platform-wide state registry."""

    __tablename__ = "platform_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON_VALUE, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
