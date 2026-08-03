"""Durable Microsoft Graph control state; no secrets or message content."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text
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


__all__ = ["GraphBinding", "GraphInboundReceipt"]
