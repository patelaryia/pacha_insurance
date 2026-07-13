"""PRD-03 evaluation and autonomy persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, JSON, DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")
TEXT_ARRAY = JSON().with_variant(ARRAY(Text()), "postgresql")


class TestCase(Base):
    """One labelled seed or production-correction corpus case."""

    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    corpus: Mapped[str] = mapped_column(Text, nullable=False, comment="'motor_v1'")
    origin: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'seed_closed_claim'|'production_correction'",
    )
    input_bundle: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment="S3 refs to docs/emails (anonymised for seed)",
    )
    expected: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment=(
            "{fields:{path:value}, rules:{id:fired}, calcs:{id:output}, "
            "note_rubric:{...}}"
        ),
    )
    tags: Mapped[list[str] | None] = mapped_column(TEXT_ARRAY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GraderRun(Base):
    """One immutable deterministic or model-grader outcome."""

    __tablename__ = "grader_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    grader_id: Mapped[str | None] = mapped_column(Text)
    subject_type: Mapped[str | None] = mapped_column(
        Text,
        comment="'field'|'rule'|'calc'|'artifact'",
    )
    subject_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    claim_id: Mapped[str | None] = mapped_column(
        Text,
        comment="one of claim/test populated",
    )
    test_case_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'pass'|'fail'|'error'",
    )
    severity: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'critical'|'major'|'minor'",
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Capability(Base):
    """One governed side-effect capability and its current autonomy level."""

    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        comment="e.g. 'intake.acknowledge','pack.note_draft'",
    )
    current_level: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="L1",
        server_default=text("'L1'"),
        comment="L0..L4",
    )
    max_level: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="hard ceiling, e.g. consistency checks = 'L2'",
    )
    policy: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment="promotion policy, 3.4",
    )


class AutonomyChange(Base):
    """Immutable evidence for a promotion or demotion."""

    __tablename__ = "autonomy_changes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    capability_id: Mapped[str | None] = mapped_column(Text)
    from_level: Mapped[str | None] = mapped_column(Text)
    to_level: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="'promotion'|'auto_demotion'|'manual'",
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        comment="run counts, pass rates, window",
    )
    approved_by: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AutonomyChange", "Capability", "GraderRun", "TestCase"]
