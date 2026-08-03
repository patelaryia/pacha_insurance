"""Durable Microsoft Graph control state; no secrets or message content."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base


class GraphBinding(Base):
    __tablename__ = "graph_bindings"

    mailbox_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    delta_token: Mapped[str | None] = mapped_column(Text)
    subscription_id: Mapped[str | None] = mapped_column(Text)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_state_sha256: Mapped[str | None] = mapped_column(Text)
    last_successful_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphInboundReceipt(Base):
    __tablename__ = "graph_inbound_receipts"

    graph_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class GraphOutboundRelease(Base):
    """Private durable state for one governed outbound Graph write."""

    __tablename__ = "graph_outbound_releases"
    __table_args__ = (
        Index("ix_graph_outbound_release_due", "status", "release_due_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    write_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    claim_id: Mapped[str] = mapped_column(Text, nullable=False)
    action_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    release_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    upload_session: Mapped[str | None] = mapped_column(Text)
    upload_attachment_id: Mapped[str | None] = mapped_column(Text)
    upload_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_message_id: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GraphOutboundRateBucket(Base):
    """Single platform-wide durable Graph send token bucket."""

    __tablename__ = "graph_outbound_rate_buckets"

    bucket_id: Mapped[str] = mapped_column(Text, primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = [
    "GraphBinding",
    "GraphInboundReceipt",
    "GraphOutboundRateBucket",
    "GraphOutboundRelease",
]
