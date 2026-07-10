"""FastAPI application factory for the Packet-1 claim substrate."""

from __future__ import annotations

import re

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from claim_core.database import (
    ClaimLocks,
    build_engine,
    build_session_factory,
    initialise_database,
)
from claim_core.errors import ClaimCoreError
from claim_core.schemas import (
    ApprovalRequiredResponse,
    ClaimCreate,
    ClaimResponse,
    ClaimStateSummary,
    DeclineRequest,
    ErrorResponse,
    FieldWriteBatch,
    FieldWriteResponse,
    HydratedClaim,
    HydratedField,
    SubstatusRequest,
    TimelineEvent,
    TimelineResponse,
    TransitionRequest,
)
from claim_core.service import ClaimService

ACTOR_PATTERN = re.compile(r"^(?:system|agent:[A-Za-z0-9._-]+|user:[A-Za-z0-9]{26})$")


def actor_header(x_actor: str = Header(alias="X-Actor")) -> str:
    """Validate the temporary Packet-1 actor transport."""

    if not ACTOR_PATTERN.fullmatch(x_actor):
        raise ClaimCoreError(422, "VALUE_TYPE_MISMATCH", "X-Actor has an invalid format")
    return x_actor


def create_app(database_url: str) -> FastAPI:
    """Create a Packet-1 app and initialise its schema."""

    engine = build_engine(database_url)
    initialise_database(engine)
    service = ClaimService(
        build_session_factory(engine),
        ClaimLocks(enabled=engine.dialect.name == "sqlite"),
    )
    app = FastAPI(title="Pacha Claim Core", version="0.1.0")
    app.state.engine = engine
    app.state.claim_service = service

    @app.exception_handler(ClaimCoreError)
    async def claim_core_error_handler(_request: Request, exc: ClaimCoreError) -> JSONResponse:
        content = ErrorResponse(code=exc.code, detail=exc.detail).model_dump()
        content.update(exc.extra)
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
        )

    @app.post("/claims", status_code=201, response_model=ClaimResponse)
    def create_claim(body: ClaimCreate, x_actor: str = Header(alias="X-Actor")) -> ClaimResponse:
        actor = actor_header(x_actor)
        return ClaimResponse.model_validate(service.create_claim(body, actor), from_attributes=True)

    @app.patch("/claims/{claim_id}/fields", response_model=FieldWriteResponse)
    def patch_fields(
        claim_id: str, body: FieldWriteBatch, x_actor: str = Header(alias="X-Actor")
    ) -> FieldWriteResponse:
        actor = actor_header(x_actor)
        return FieldWriteResponse(results=service.write_fields(claim_id, body.writes, actor))

    @app.get("/claims/{claim_id}", response_model=HydratedClaim)
    def get_claim(claim_id: str, x_actor: str = Header(alias="X-Actor")) -> HydratedClaim:
        actor_header(x_actor)
        claim, current, blocked_reasons = service.hydrate_claim(claim_id)
        base = ClaimResponse.model_validate(claim, from_attributes=True).model_dump()
        fields = {
            path: HydratedField.model_validate(field, from_attributes=True)
            for path, field in current.items()
        }
        return HydratedClaim(**base, fields=fields, blocked_reasons=blocked_reasons)

    @app.post("/claims/{claim_id}/transition", response_model=ClaimStateSummary)
    def transition_claim(
        claim_id: str, body: TransitionRequest, x_actor: str = Header(alias="X-Actor")
    ) -> ClaimStateSummary:
        actor = actor_header(x_actor)
        result = service.transition_claim(claim_id, body.to, body.payload, actor)
        return ClaimStateSummary(
            id=result.claim_id,
            status=result.status.value,
            substatus=result.substatus,
        )

    @app.post(
        "/claims/{claim_id}/decline",
        response_model=ClaimStateSummary | ApprovalRequiredResponse,
    )
    def decline_claim(
        claim_id: str, body: DeclineRequest, x_actor: str = Header(alias="X-Actor")
    ) -> ClaimStateSummary | JSONResponse:
        actor = actor_header(x_actor)
        result = service.decline_claim(claim_id, body.reason, actor)
        if result.approval_required:
            return JSONResponse(status_code=202, content={"code": "APPROVAL_REQUIRED"})
        return ClaimStateSummary(
            id=result.claim_id,
            status=result.status.value,
            substatus=result.substatus,
        )

    @app.post("/claims/{claim_id}/substatus", response_model=ClaimStateSummary)
    def set_claim_substatus(
        claim_id: str, body: SubstatusRequest, x_actor: str = Header(alias="X-Actor")
    ) -> ClaimStateSummary:
        actor = actor_header(x_actor)
        result = service.set_claim_substatus(claim_id, body.substatus, actor)
        return ClaimStateSummary(
            id=result.claim_id,
            status=result.status.value,
            substatus=result.substatus,
        )

    @app.get("/claims/{claim_id}/timeline", response_model=TimelineResponse)
    def get_timeline(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> TimelineResponse:
        actor_header(x_actor)
        return TimelineResponse(
            events=[
                TimelineEvent.model_validate(event, from_attributes=True)
                for event in service.timeline(claim_id)
            ]
        )

    return app
