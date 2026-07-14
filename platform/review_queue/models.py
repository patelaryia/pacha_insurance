"""Persistence model for the rebuildable PRD-04 review queue projection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base
from cop_runtime.contracts import REVIEW_ITEM_TYPES

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


def _quoted(values: frozenset[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


class ReviewItem(Base):
    """One materialised review.created event and its eventual decision."""

    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint(
            f"type IN ({_quoted(REVIEW_ITEM_TYPES)})",
            name="ck_review_items_type",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved', 'cancelled')",
            name="ck_review_items_status",
        ),
        UniqueConstraint("source_event_id", name="uq_review_items_source_event_id"),
        {"comment": "Rebuildable projection of review.created events (PRD-04)."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str | None] = mapped_column(
        Text, comment="nullable for platform-wide promotion sign-offs"
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    subtype: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, comment="verbatim producing-event payload"
    )
    source_event_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="idempotency key"
    )
    assigned_to: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolution_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    resolution_schema_version: Mapped[str | None] = mapped_column(Text)


__all__ = ["ReviewItem"]
