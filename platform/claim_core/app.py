"""FastAPI application factory for the Packet-1 claim substrate."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from claim_core.celery_app import configure_runtime
from claim_core.consumers import ExternalRefsConsumer
from claim_core.crypto import KeyProvider, LocalKeyProvider
from claim_core.database import (
    ClaimLocks,
    build_engine,
    build_session_factory,
    initialise_database,
)
from claim_core.errors import ClaimCoreError
from claim_core.ledger import LedgerWriter
from claim_core.outbox import Dispatcher
from claim_core.schemas import (
    ApprovalRequiredResponse,
    ClaimCreate,
    ClaimListItem,
    ClaimListResponse,
    ClaimResponse,
    ClaimStateSummary,
    DeclineRequest,
    DocumentCreated,
    DocumentListResponse,
    DocumentMetadata,
    ErrorResponse,
    FieldWriteBatch,
    FieldWriteResponse,
    HydratedClaim,
    HydratedField,
    ReplayEvent,
    ReplayResponse,
    SubstatusRequest,
    TimelineEvent,
    TimelineResponse,
    TransitionRequest,
)
from claim_core.service import ClaimService, utc_now
from claim_core.sla import SlaEngine
from claim_core.storage import BlobStore, LocalBlobStore

ACTOR_PATTERN = re.compile(r"^(?:system|agent:[A-Za-z0-9._-]+|user:[A-Za-z0-9]{26})$")


def actor_header(x_actor: str = Header(alias="X-Actor")) -> str:
    """Validate the temporary Packet-1 actor transport."""

    if not ACTOR_PATTERN.fullmatch(x_actor):
        raise ClaimCoreError(422, "VALUE_TYPE_MISMATCH", "X-Actor has an invalid format")
    return x_actor


def create_app(
    database_url: str,
    *,
    clock: Callable[[], datetime] | None = None,
    key_provider: KeyProvider | None = None,
    blob_store: BlobStore | None = None,
) -> FastAPI:
    """Create the complete, synchronous-drivable PRD-00 application."""

    effective_clock = clock or utc_now
    effective_keys = key_provider or LocalKeyProvider.from_environment()
    effective_blobs = blob_store or LocalBlobStore(tempfile.mkdtemp(prefix="pacha-blobs-"))
    engine = build_engine(database_url)
    initialise_database(engine)
    session_factory = build_session_factory(engine)
    service = ClaimService(
        session_factory,
        ClaimLocks(enabled=engine.dialect.name == "sqlite"),
        effective_clock,
        key_provider=effective_keys,
        blob_store=effective_blobs,
    )
    ledger = LedgerWriter(
        session_factory,
        effective_blobs,
        clock=effective_clock,
        event_recorder=service.record_event,
    )
    service.set_ledger(ledger)
    sla_engine = SlaEngine(
        session_factory, service.record_event, clock=effective_clock
    )
    dispatcher = Dispatcher(
        session_factory, service.record_event, clock=effective_clock
    )
    dispatcher.register_consumer("ledger", ledger.consume)
    dispatcher.register_consumer("external_refs", ExternalRefsConsumer(session_factory))
    dispatcher.register_consumer("sla", sla_engine.consume)
    configure_runtime(dispatcher=dispatcher, sla_engine=sla_engine, ledger=ledger)
    app = FastAPI(title="Pacha Claim Core", version="0.1.0")
    app.state.engine = engine
    app.state.claim_service = service
    app.state.dispatcher = dispatcher
    app.state.sla_engine = sla_engine
    app.state.ledger = ledger

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

    @app.get("/claims", response_model=ClaimListResponse)
    def list_claims(
        status: str | None = None,
        lob: str | None = None,
        sla_breached: bool | None = None,
        x_actor: str = Header(alias="X-Actor"),
    ) -> ClaimListResponse:
        actor_header(x_actor)
        return ClaimListResponse(
            claims=[
                ClaimListItem.model_validate(claim, from_attributes=True)
                for claim in service.list_claims(
                    status=status, lob=lob, sla_breached=sla_breached
                )
            ]
        )

    @app.get("/claims/{claim_id}", response_model=HydratedClaim)
    def get_claim(claim_id: str, x_actor: str = Header(alias="X-Actor")) -> HydratedClaim:
        actor = actor_header(x_actor)
        claim, current, blocked_reasons = service.hydrate_claim(claim_id, actor)
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

    @app.post(
        "/claims/{claim_id}/documents",
        status_code=201,
        response_model=DocumentCreated,
    )
    async def upload_document(
        claim_id: str,
        file: Annotated[UploadFile, File()],
        source_channel: Annotated[str, Form()],
        source_ref: Annotated[str, Form()],
        x_actor: str = Header(alias="X-Actor"),
    ) -> DocumentCreated:
        actor = actor_header(x_actor)
        row = service.add_document(
            claim_id,
            filename=file.filename or "unnamed",
            mime=file.content_type or "application/octet-stream",
            content=await file.read(),
            source_channel=source_channel,
            source_ref=source_ref,
            actor=actor,
        )
        return DocumentCreated.model_validate(row, from_attributes=True)

    @app.get(
        "/claims/{claim_id}/documents", response_model=DocumentListResponse
    )
    def get_documents(
        claim_id: str, x_actor: str = Header(alias="X-Actor")
    ) -> DocumentListResponse:
        actor_header(x_actor)
        return DocumentListResponse(
            documents=[
                DocumentMetadata.model_validate(row, from_attributes=True)
                for row in service.documents(claim_id)
            ]
        )

    @app.get("/events", response_model=ReplayResponse)
    def replay_events(
        after_seq: int = Query(default=0, ge=0),
        x_actor: str = Header(alias="X-Actor"),
    ) -> ReplayResponse:
        actor_header(x_actor)
        return ReplayResponse(
            events=[
                ReplayEvent.model_validate(event, from_attributes=True)
                for event in service.replay(after_seq)
            ]
        )

    return app
