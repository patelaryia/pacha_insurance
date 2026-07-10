"""Durable, resumable document-pipeline stage state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from claim_core import Base

STAGES = (
    "NORMALIZE",
    "CLASSIFY",
    "SPLIT",
    "EXTRACT",
    "CITE",
    "VALIDATE",
    "COMMIT",
    "CONSISTENCY",
)
TERMINAL_STAGE_STATUSES = frozenset({"succeeded", "skipped"})


class DocumentStage(Base):
    """One durable execution slot for one document pipeline stage."""

    __tablename__ = "document_stages"
    __table_args__ = (
        UniqueConstraint("document_id", "stage", name="uq_document_stages_document_stage"),
        CheckConstraint(
            "stage IN ('NORMALIZE','CLASSIFY','SPLIT','EXTRACT','CITE','VALIDATE',"
            "'COMMIT','CONSISTENCY')",
            name="ck_document_stages_stage",
        ),
        CheckConstraint(
            "status IN ('pending','succeeded','failed','skipped')",
            name="ck_document_stages_status",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="ULID")
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    output_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class StageResult:
    """Result returned by a Celery-wrappable stage callable."""

    status: str
    output_ref: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(frozen=True)
class PipelineOutcome:
    """Stable caller-facing outcome for one document processing attempt."""

    document_id: str
    stages: dict[str, str]
    committed_paths: list[str]
    review_items: list[dict[str, Any]]
    failed: bool
