"""PRD-06 checklist persistence on the shared claim-core metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base


class ChaseChecklist(Base):
    """One durable collection purpose for a claim."""

    __tablename__ = "chase_checklists"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('claim_docs', 'surrender', 'assessor_report')",
            name="ck_chase_checklists_purpose",
        ),
        CheckConstraint(
            "status IN ('open', 'complete', 'cancelled')",
            name="ck_chase_checklists_status",
        ),
        {"comment": "Never-purged PRD-06 checklist evidence."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    blocking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    requester_party_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("parties.id", name="fk_chase_checklists_requester_party_id"),
        comment="Explicit requester for assessor-report checklists; null uses intimation sender.",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChaseItem(Base):
    """One registered checklist item and its monotonic chase evidence."""

    __tablename__ = "chase_items"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','requested','received','verified','rejected','waived')",
            name="ck_chase_items_state",
        ),
        {"comment": "Never-purged per-item document-cycle evidence."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    checklist_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chase_checklists.id"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    physical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waived_by: Mapped[str | None] = mapped_column(Text)
    waiver_reason: Mapped[str | None] = mapped_column(Text)
    reminder_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_id: Mapped[str | None] = mapped_column(Text, ForeignKey("documents.id"))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["ChaseChecklist", "ChaseItem"]
