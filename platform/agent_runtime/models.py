"""Durable AR-1 agent execution records — the Temporal operational projection.

Section 0.5 AR-1 (as amended by register #284 and master plan §13) makes this
table a *projection* of Workflow position, not claim truth. PostgreSQL stays
authoritative for claims, events and the audit ledger; this row records where a
run's Workflow is, so the console and the APIs can answer "what is happening"
without querying Temporal.

`workflow_id` is the stable Pacha-minted Workflow identity and is unique, which
is what makes a retried start attach to the existing execution rather than
duplicating domain work. `workflow_run_id` changes on Continue-As-New and is
therefore deliberately nullable and mutable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base
from claim_core.types import JSON_VALUE

#: Master plan §13 — the closed seven-value status set, DDL-level constitution.
AGENT_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "awaiting_review",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)

_STATUS_CHECK = "status IN (" + ", ".join(f"'{status}'" for status in AGENT_RUN_STATUSES) + ")"


class AgentRun(Base):
    """One durable execution whose id is the correlation id for child events."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_agent_runs_status"),
        UniqueConstraint("workflow_id", name="uq_agent_runs_workflow_id"),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_claim", "claim_id"),
        {"comment": "Durable AR-1 agent execution record."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID correlation id")
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(Text)
    trigger_event: Mapped[str | None] = mapped_column(Text, ForeignKey("events.id"))
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(Text)
    workflow_type: Mapped[str] = mapped_column(Text, nullable=False)
    worker_build_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    last_workflow_event_ref: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["AGENT_RUN_STATUSES", "AgentRun"]
