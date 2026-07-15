"""Durable AR-1 agent execution records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base
from claim_core.types import JSON_VALUE


class AgentRun(Base):
    """One durable execution whose id is the correlation id for child events."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'awaiting_review', 'completed', 'failed', 'blocked')",
            name="ck_agent_runs_status",
        ),
        {"comment": "Durable AR-1 agent execution record."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID correlation id")
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(Text)
    trigger_event: Mapped[str | None] = mapped_column(Text, ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["AgentRun"]
