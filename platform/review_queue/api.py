"""FastAPI routes for the PRD-04 review queue substrate."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from review_queue.service import ReviewService


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit_approve", "reject"]
    schema_version: str = Field(min_length=1)
    payload: dict[str, Any]


def build_router(service: ReviewService) -> APIRouter:
    router = APIRouter()

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

    return router


__all__ = ["ResolveRequest", "build_router"]
