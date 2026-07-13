"""Append-only rule and calculation execution records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base


class RuleRun(Base):
    """One reconstructable rule evaluation."""

    __tablename__ = "rule_runs"
    __table_args__ = {"comment": "Append-only COP rule evaluation history."}

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    pack_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fired: Mapped[bool | None] = mapped_column(Boolean)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    missing_inputs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalcRun(Base):
    """One reconstructable pack calculation execution."""

    __tablename__ = "calc_runs"
    __table_args__ = {"comment": "Append-only COP calculation execution history."}

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    calc_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output: Mapped[Any | None] = mapped_column(JSON)
    claim_id: Mapped[str] = mapped_column(Text, ForeignKey("claims.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    pack_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    missing_inputs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = ["CalcRun", "RuleRun"]
