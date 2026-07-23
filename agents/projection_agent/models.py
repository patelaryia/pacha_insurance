"""PRD-09 §9.2 projection persistence on the shared claim-core metadata.

The DDL is binding (register #261): no convenience columns, no foreign keys,
no soft-delete flag, no job lease, and no target-system identifier. Mode and
status vocabularies are closed by service validation, exactly as PRD-09 §9.2
records them in column comments.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, Text, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base

JSON_VALUE = JSON().with_variant(postgresql.JSONB(), "postgresql")

MODES = frozenset({"paste_assist", "rpa", "api"})
STATUSES = frozenset(
    {"queued", "executing", "verifying", "completed", "failed", "diverged"}
)
#: PACKET-20 owns only the paste-assist forward edges. `failed`/`diverged` are
#: PACKET-21's, except the structural fail-closed edge in `service.py`.
LEGAL_EDGES = frozenset(
    {
        ("queued", "executing"),
        ("executing", "executing"),
        ("executing", "verifying"),
        ("verifying", "completed"),
    }
)


class Projection(Base):
    """One immutable projection snapshot and its mutable lifecycle columns."""

    __tablename__ = "projections"
    __table_args__ = (
        {"comment": "PRD-09 §9.2 projections; payload snapshots are never mutated."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(
        Text, nullable=False, comment="paste_assist|rpa|api"
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="queued|executing|verifying|completed|failed|diverged",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment="field paths + values + versions (snapshot)",
    )
    readback: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    divergence: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_VALUE,
        comment="screenshot s3 keys per step (rpa), confirm ts + user (paste_assist)",
    )
    attempts: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["LEGAL_EDGES", "MODES", "Projection", "STATUSES"]
