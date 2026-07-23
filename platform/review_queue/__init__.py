"""Public builder for the PRD-04 review queue substrate."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import sessionmaker

from claim_core import Base
from review_queue.api import build_router
from review_queue.auth import install_console
from review_queue.contracts import ContractRegistry
from review_queue.models import ReviewItem
from review_queue.ops_api import build_ops_router
from review_queue.projection import ReviewProjection
from review_queue.rbac import Authorizer, load_authority_matrix, load_roles
from review_queue.service import ReviewService


class ReviewQueue:
    """Application-scoped queue projection and resolution facade."""

    def __init__(self, projection: ReviewProjection, service: ReviewService) -> None:
        self._projection = projection
        self.service = service

    def backfill(self, actor: str) -> None:
        self._projection.backfill(actor)

    def cancel(self, review_id: str, *, actor: str, reason: str) -> dict[str, object]:
        """Withdraw one producer-owned superseded approval-note review."""

        return self.service.cancel(review_id, actor=actor, reason=reason)

    @property
    def roles(self) -> MappingProxyType[str, str]:
        """Read-only organisation roles used by the installed console."""

        return MappingProxyType(dict(self.service.authorizer.roles))

    @property
    def contracts(self):
        """Read-only workspace and resolution contract metadata."""

        return self.service.contracts.metadata


def build_review_queue(
    app: Any,
    *,
    roles: dict[str, str] | None = None,
    contracts_path: str | Path | None = None,
) -> ReviewQueue:
    """Build the projection, validate pack contracts, and attach API routes."""

    repo = Path(__file__).resolve().parents[2]
    review_dir = Path(contracts_path) if contracts_path is not None else repo / "packs/motor/review"
    routing_dir = repo / "packs/motor/routing"
    contracts = ContractRegistry(review_dir)
    configured_roles = load_roles(routing_dir / "roles.yaml") if roles is None else dict(roles)
    authorizer = Authorizer(
        configured_roles,
        load_authority_matrix(routing_dir / "authority_matrix.yaml"),
    )
    sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
    Base.metadata.create_all(app.state.engine, tables=[ReviewItem.__table__])
    projection = ReviewProjection(sessions)
    service = ReviewService(app, sessions, contracts, authorizer)
    queue = ReviewQueue(projection, service)
    app.state.review_queue = queue
    app.state.dispatcher.register_consumer("review_queue", projection.consume)
    app.include_router(build_router(service))
    return queue


def install_ops(app: Any) -> Any:
    """Install the pinned S-3–S-6 operations API after queue construction."""

    if not hasattr(app.state, "review_queue"):
        raise ValueError("build_review_queue must run before install_ops")
    if not hasattr(app.state, "notify"):
        raise ValueError("build_notify must run before install_ops")
    app.include_router(build_ops_router(app))
    return app


__all__ = ["ReviewQueue", "build_review_queue", "install_console", "install_ops"]
