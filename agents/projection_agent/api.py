"""Authenticated PRD-09 paste-assist routes. No portal or public surface.

The client can never supply a payload, hash, field version, mode, or
idempotency key material other than the request header: every value on the
strip is rebuilt server-side from the immutable snapshot.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from claim_core import ClaimCoreError

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


class PasteReadbackCapture(BaseModel):
    """One officer-observed target capture (PACKET-21 §12).

    Values never reach the review item, the event, or the log: the service
    protects them under the claim DEK and returns only an opaque capture id.
    """

    model_config = ConfigDict(extra="forbid")

    observed: dict[str, Any] = Field(default_factory=dict)
    screenshot_base64: str | None = None


def build_router(service: Any) -> APIRouter:
    """Return the eight authenticated projection routes."""

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

    @router.get("/{projection_id}/rpa")
    def read_rpa(
        claim_id: str, projection_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> JSONResponse:
        return _private(service.rpa_view(claim_id, projection_id, actor=x_actor))

    @router.get("/{projection_id}/evidence/{evidence_id}")
    def read_evidence(
        claim_id: str,
        projection_id: str,
        evidence_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> Response:
        content, digest = service.read_evidence(
            claim_id, projection_id, evidence_id, actor=x_actor
        )
        return Response(
            content=content,
            media_type="image/png",
            headers={**PRIVATE_HEADERS, "X-Evidence-SHA256": digest},
        )

    return router


def build_review_router(service: Any) -> APIRouter:
    """The one authenticated review-scoped projection route (PACKET-21 §12)."""

    router = APIRouter(prefix="/console/reviews")

    @router.post("/{review_id}/paste-readback/capture")
    def capture_paste_readback(
        review_id: str,
        body: PasteReadbackCapture,
        x_actor: str = Header(alias="X-Actor"),
    ) -> JSONResponse:
        screenshot: bytes | None = None
        if body.screenshot_base64:
            try:
                screenshot = base64.b64decode(body.screenshot_base64, validate=True)
            except (ValueError, TypeError) as error:
                raise ClaimCoreError(
                    422, "EVIDENCE_CONTENT_INVALID", "Screenshot bytes are not valid base64"
                ) from error
        return _private(
            service.capture_paste_readback(
                review_id,
                actor=x_actor,
                observed=body.observed,
                screenshot=screenshot,
            )
        )

    return router


__all__ = ["PRIVATE_HEADERS", "build_review_router", "build_router"]
