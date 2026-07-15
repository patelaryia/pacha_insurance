"""FastAPI routes for the PRD-04 review queue substrate."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from review_queue.ops_reads import OpsReadService
from review_queue.service import ReviewService

PORTFOLIO_ROLES = frozenset(
    {"claims_manager", "gm", "md", "chairman", "head_of_claims", "auditor"}
)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit_approve", "reject"]
    schema_version: str = Field(min_length=1)
    payload: dict[str, Any]


def build_router(service: ReviewService) -> APIRouter:
    router = APIRouter()
    ops_reads = OpsReadService(service.app)

    def require_portfolio(actor: str) -> None:
        role = service.authorizer.role(actor)
        if role not in PORTFOLIO_ROLES:
            from claim_core import ClaimCoreError

            raise ClaimCoreError(
                403, "FORBIDDEN_ROLE", "Role is not authorised for portfolio reads"
            )

    @router.get("/reviews")
    def list_reviews(
        scope: Literal["mine", "pool", "band"] = Query(default="mine"),
        type_name: str | None = Query(default=None, alias="type"),
        status: str | None = None,
        claim_id: str | None = None,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return {
            "items": service.list_items(
                actor=x_actor,
                scope=scope,
                type_name=type_name,
                status=status,
                claim_id=claim_id,
            )
        }

    @router.get("/reviews/{review_id}")
    def get_review(
        review_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return service.get_item(review_id, actor=x_actor)

    @router.post("/reviews/{review_id}/resolve")
    def resolve_review(
        review_id: str,
        body: ResolveRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return service.resolve(
            review_id,
            actor=x_actor,
            action=body.action,
            schema_version=body.schema_version,
            payload=body.payload,
        )

    @router.get("/portfolio")
    def portfolio(x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        require_portfolio(x_actor)
        return {"tiles": ops_reads.portfolio()}

    @router.get("/portfolio/{series_id}.csv")
    def portfolio_csv(
        series_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> Response:
        require_portfolio(x_actor)
        return Response(content=ops_reads.csv(series_id), media_type="text/csv")

    return router


__all__ = ["ResolveRequest", "build_router"]
