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
#: PACKET-20 owns the paste-assist forward edges; PACKET-21 §2 adds the RPA
#: edges, the safe pre-write fallback, and the two divergence entries. A
#: terminal `failed|diverged` row never returns to execution, so no edge leaves
#: those states.
LEGAL_EDGES = frozenset(
    {
        ("queued", "executing"),
        ("executing", "executing"),
        ("executing", "verifying"),
        ("verifying", "completed"),
        # PACKET-21 §2.
        ("executing", "queued"),  # safe pre-write RPA failure, same row
        ("executing", "failed"),  # known terminal failure or uncertain write
        ("verifying", "diverged"),  # immediate RPA readback mismatch
        ("completed", "diverged"),  # sampled paste mismatch or standing drift
    }
)
#: Rows that may never be executed or mutated again.
TERMINAL_STATUSES = frozenset({"failed", "diverged"})
#: Rows whose `completed_at` must be set.
CLOSED_STATUSES = frozenset({"completed", "failed", "diverged"})


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


__all__ = [
    "CLOSED_STATUSES",
    "LEGAL_EDGES",
    "MODES",
    "Projection",
    "STATUSES",
    "TERMINAL_STATUSES",
]
