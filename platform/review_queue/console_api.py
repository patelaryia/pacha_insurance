"""FastAPI routes for trusted console identity and Claim-360 reads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Response

from review_queue.console_reads import ConsoleReadService


def build_console_router(app: Any) -> APIRouter:
    router = APIRouter()
    reads = ConsoleReadService(app)

    @router.get("/auth/me")
    def auth_me(x_actor: str = Header(alias="X-Actor")) -> dict[str, str]:
        return {"actor": x_actor, "role": app.state.console_roles[x_actor]}

    @router.get("/console/claims/{claim_id}/360")
    def claim_360(
        claim_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return reads.claim_360(claim_id, x_actor)

    @router.get("/console/claims/{claim_id}/fields/{field_path}/citation")
    def field_citation(
        claim_id: str,
        field_path: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        return reads.citation(claim_id, field_path, x_actor)

    @router.get("/console/documents/{document_id}/normalised.pdf")
    def normalised_pdf(
        document_id: str,
        _x_actor: str = Header(alias="X-Actor"),
    ) -> Response:
        return Response(
            content=reads.normalised_pdf(document_id),
            media_type="application/pdf",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


__all__ = ["build_console_router"]
