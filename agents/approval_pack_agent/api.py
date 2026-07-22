"""Authenticated PRD-08 approval-pack routes. No portal or public surface."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

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


def build_router(service: Any) -> APIRouter:
    """Return the four PRD-08 routes plus the PACKET-19 read surface."""

    router = APIRouter(prefix="/claims/{claim_id}/approval-pack")

    @router.get("/readiness")
    def read_readiness(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> dict[str, Any]:
        service.require_role(x_actor)
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

    return router


__all__ = ["build_router", "ClaimCoreError"]
