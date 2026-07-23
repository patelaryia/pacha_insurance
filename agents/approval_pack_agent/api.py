"""Authenticated PRD-08 approval-pack routes. No portal or public surface."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from claim_core import ClaimCoreError


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    id: str = Field(min_length=1)


class SourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceRef] = Field(min_length=1)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readiness_fingerprint: str = Field(min_length=1)


class CommentaryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_slot: str = Field(min_length=1)
    content: str


class AutosaveRequest(BaseModel):
    """PACKET-19 §3: only prose crosses the wire, never a locked section."""

    model_config = ConfigDict(extra="forbid")

    base_draft_id: str = Field(min_length=1)
    base_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commentary: list[CommentaryEdit] = Field(min_length=1)


def build_review_router(service: Any) -> APIRouter:
    """Return the authenticated NOTE_REVIEW workspace read and autosave."""

    router = APIRouter(prefix="/reviews/{review_id}/approval-note")

    @router.get("")
    def read_workspace(
        review_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> dict[str, Any]:
        return service.workspace.read(review_id, x_actor)

    @router.put("/draft")
    def autosave_draft(
        review_id: str,
        body: AutosaveRequest,
        x_actor: str = Header(alias="X-Actor"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return service.workspace.autosave(
            review_id,
            actor=x_actor,
            idempotency_key=idempotency_key,
            base_draft_id=body.base_draft_id,
            base_body_sha256=body.base_body_sha256,
            commentary=[entry.model_dump() for entry in body.commentary],
        )

    return router


def build_router(service: Any) -> APIRouter:
    """Return the four PRD-08 routes plus the PACKET-19 read surface."""

    router = APIRouter(prefix="/claims/{claim_id}/approval-pack")

    @router.get("/readiness")
    def read_readiness(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> dict[str, Any]:
        service.require_read_role(x_actor)
        return service.readiness.evaluate(claim_id, x_actor).card()

    @router.put("/manifest/{item_id}/sources")
    def select_sources(
        claim_id: str,
        item_id: str,
        body: SourceSelection,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return service.select_sources(
            claim_id,
            item_id,
            [source.model_dump() for source in body.sources],
            x_actor,
        )

    @router.post("/manifest/{item_id}/upload", status_code=201)
    async def upload_item(
        claim_id: str,
        item_id: str,
        file: Annotated[UploadFile, File()],
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return service.upload_item(
            claim_id,
            item_id,
            filename=file.filename or "upload.pdf",
            mime=file.content_type or "application/pdf",
            content=await file.read(),
            actor=x_actor,
        )

    @router.post("/generate")
    def generate(
        claim_id: str,
        body: GenerateRequest,
        x_actor: str = Header(alias="X-Actor"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JSONResponse:
        status_code, payload = service.generate(
            claim_id,
            actor=x_actor,
            idempotency_key=idempotency_key,
            fingerprint=body.readiness_fingerprint,
        )
        return JSONResponse(status_code=status_code, content=payload)

    @router.get("/versions")
    def read_versions(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> dict[str, Any]:
        return {"versions": service.versions(claim_id, x_actor)}

    @router.get("/note-drafts")
    def read_note_drafts(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> dict[str, Any]:
        return {"note_drafts": service.note_drafts(claim_id, x_actor)}

    @router.get("/artifacts/{event_id}", response_class=Response)
    def read_artifact(
        claim_id: str, event_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> Response:
        # #253: the browser sends an allowlisted event id; the blob key is
        # resolved server-side and never accepted from the client.
        artifact = service.workspace.artifact(claim_id, event_id, x_actor)
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{artifact["filename"]}"',
        }
        if artifact["sha256"] is not None:
            headers["ETag"] = f'"{artifact["sha256"]}"'
        return Response(
            content=artifact["content"],
            media_type="application/pdf",
            headers=headers,
        )

    return router


__all__ = ["build_review_router", "build_router", "ClaimCoreError"]
