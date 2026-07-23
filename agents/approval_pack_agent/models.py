"""PRD-08 §8.5 approval-note persistence on the shared claim-core metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base

JSON_VALUE = JSON().with_variant(postgresql.JSONB(), "postgresql")


class NoteDraft(Base):
    """One approval-note version. The DDL is binding: no convenience columns."""

    __tablename__ = "note_drafts"
    __table_args__ = (
        UniqueConstraint("claim_id", "version", name="uq_note_drafts_claim_version"),
        CheckConstraint(
            "status IN ('draft', 'in_review', 'signed', 'superseded')",
            name="ck_note_drafts_status",
        ),
        CheckConstraint("version >= 1", name="ck_note_drafts_version"),
        {"comment": "Append-only PRD-08 approval-note draft versions."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(
        Text, ForeignKey("claims.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, comment="sections[] etc"
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, comment="draft|in_review|signed|superseded"
    )
    edited_by: Mapped[str | None] = mapped_column(Text)
    signed_by: Mapped[str | None] = mapped_column(Text)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["NoteDraft"]
