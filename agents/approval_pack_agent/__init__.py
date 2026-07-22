"""Public PRD-08 approval-pack agent boundary (register #219).

Install after ``build_agent_runtime``, ``build_eval_harness``, and
``build_review_queue``. The installer is idempotent and exposes the curated
service as ``app.state.approval_pack_agent``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from approval_pack_agent.api import build_router
from approval_pack_agent.config import ApprovalPackConfig, PackConfigError, load_config
from approval_pack_agent.conversion import (
    HtmlPdfRenderer,
    ImmutableArtifactStore,
    LocalImmutableStore,
)
from approval_pack_agent.models import NoteDraft
from approval_pack_agent.note import guard_note_review
from approval_pack_agent.service import ApprovalPackService
from assessment_agent import savings_tables
from chase_agent import checklist_tables
from claim_core import Base

ApprovalPackAgent = ApprovalPackService


def note_draft_tables() -> tuple[Any, ...]:
    """Expose the PRD-08 note-draft table to Alembic and dependent packages."""

    return (NoteDraft.__table__,)


def build_approval_pack_agent(
    app: Any,
    *,
    model_client: Any = None,
    html_renderer: Any = None,
    immutable_store: Any = None,
    config: dict[str, Any] | None = None,
) -> ApprovalPackService:
    """Build PRD-08 after the shared runtime, eval harness, and review queue."""

    existing = getattr(app.state, "approval_pack_agent", None)
    if existing is not None:
        return existing
    for dependency in ("agent_runtime", "eval_harness", "review_queue", "cop_runtime"):
        if not hasattr(app.state, dependency):
            raise RuntimeError(
                "build_approval_pack_agent requires the shared runtime, eval harness, "
                "review queue, and COP runtime"
            )
    if model_client is None or not callable(getattr(model_client, "structured_call", None)):
        raise ValueError("approval pack agent requires a structured model client")
    if html_renderer is None or not callable(getattr(html_renderer, "render", None)):
        raise ValueError("approval pack agent requires an offline HTML renderer")
    repo = Path(__file__).resolve().parents[2]
    configured = load_config(repo / "packs" / "motor", config)
    Base.metadata.create_all(
        app.state.engine,
        tables=[*note_draft_tables(), *checklist_tables(), *savings_tables()],
    )
    service = ApprovalPackService(
        app,
        configured,
        model_client=model_client,
        html_renderer=html_renderer,
        immutable_store=immutable_store,
    )
    app.state.agent_runtime.register_executor("pack.merge", service.execute_merge)
    app.state.agent_runtime.register_executor("pack.note_draft", service.execute_note_draft)
    app.state.review_queue.service.register_resolution_validator(
        "NOTE_REVIEW", guard_note_review
    )
    app.state.dispatcher.register_consumer("approval_pack", service.consume)
    app.include_router(build_router(service))
    app.state.approval_pack_agent = service
    return service


__all__ = [
    "ApprovalPackAgent",
    "ApprovalPackConfig",
    "HtmlPdfRenderer",
    "ImmutableArtifactStore",
    "LocalImmutableStore",
    "PackConfigError",
    "build_approval_pack_agent",
    "note_draft_tables",
]
