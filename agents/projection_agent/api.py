"""Authenticated PRD-09 paste-assist routes. No portal or public surface.

The client can never supply a payload, hash, field version, mode, or
idempotency key material other than the request header: every value on the
strip is rebuilt server-side from the immutable snapshot.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    done: bool


class ConfirmRequest(BaseModel):
    """`attested` stays untyped so the service can require literal ``true``.

    Declaring it ``bool`` would let Pydantic coerce ``1`` or ``"true"`` into an
    attestation the officer never made.
    """

    model_config = ConfigDict(extra="forbid")

    attested: Any = None
    readback: dict[str, Any] = Field(default_factory=dict)


def _private(payload: Any) -> JSONResponse:
    return JSONResponse(content=payload, headers=PRIVATE_HEADERS)


def build_router(service: Any) -> APIRouter:
    """Return the five authenticated projection routes."""

    router = APIRouter(prefix="/console/claims/{claim_id}/projections")

    @router.get("")
    def list_projections(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> JSONResponse:
        return _private(service.claim_surface(claim_id, actor=x_actor))

    @router.get("/{projection_id}/paste-assist")
    def read_paste_assist(
        claim_id: str, projection_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> JSONResponse:
        return _private(service.paste_view(claim_id, projection_id, actor=x_actor))

    @router.post("/{projection_id}/paste-assist/start")
    def start_paste_assist(
        claim_id: str, projection_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> JSONResponse:
        return _private(service.start(claim_id, projection_id, actor=x_actor))

    @router.put("/{projection_id}/paste-assist/groups/{group_id}")
    def update_group(
        claim_id: str,
        projection_id: str,
        group_id: str,
        body: GroupUpdate,
        x_actor: str = Header(alias="X-Actor"),
    ) -> JSONResponse:
        return _private(
            service.set_group(
                claim_id, projection_id, group_id, done=body.done, actor=x_actor
            )
        )

    @router.post("/{projection_id}/paste-assist/confirm")
    def confirm_paste_assist(
        claim_id: str,
        projection_id: str,
        body: ConfirmRequest,
        x_actor: str = Header(alias="X-Actor"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JSONResponse:
        return _private(
            service.confirm(
                claim_id,
                projection_id,
                actor=x_actor,
                idempotency_key=idempotency_key,
                attested=body.attested,
                readback=body.readback,
            )
        )

    return router


__all__ = ["PRIVATE_HEADERS", "build_router"]
