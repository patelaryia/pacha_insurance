"""Append-only PRD-07 assessment savings ledger model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, Computed, DateTime, ForeignKey, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base

JSON_VALUE = JSON().with_variant(postgresql.JSONB(), "postgresql")


class SavingsLedger(Base):
    """Contract-billable header savings and non-billable line evidence."""

    __tablename__ = "savings_ledger"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('assessment_negotiation', 'supplier_substitution', "
            "'salvage_recovery')",
            name="ck_savings_ledger_kind",
        ),
        {"comment": "Append-only PRD-07 savings ledger; rows are never deleted."},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(
        Text, ForeignKey("claims.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    achieved_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saving: Mapped[int] = mapped_column(
        BigInteger,
        Computed("baseline_amount - achieved_amount", persisted=True),
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    vendor_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


__all__ = ["SavingsLedger"]
