"""Officer checklist controls and read surface for PRD-06."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from chase_agent.checklist import ChecklistService, aware
from chase_agent.models import ChaseChecklist, ChaseItem
from claim_core import ClaimCoreError, FieldWrite

CHASE_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "head_of_claims",
        "gm",
        "md",
        "chairman",
    }
)
ATTESTED_FIELDS = {
    "logbook_original": "salvage.logbook_held",
    "keys_physical": "salvage.keys_held",
}


class WaiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


class SnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: datetime


def build_router(app: Any, service: ChecklistService) -> APIRouter:
    router = APIRouter(prefix="/chase")

    def role(actor: str) -> str:
        value = app.state.review_queue.service.authorizer.role(actor)
        if value not in CHASE_ROLES:
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Role cannot manage chase items")
        return str(value)

    def item_context(item_id: str) -> tuple[ChaseChecklist, ChaseItem]:
        with service.sessions() as session:
            row = session.execute(
                select(ChaseChecklist, ChaseItem)
                .join(ChaseItem, ChaseItem.checklist_id == ChaseChecklist.id)
                .where(ChaseItem.id == item_id)
            ).first()
            if row is None:
                raise ClaimCoreError(404, "CHASE_ITEM_NOT_FOUND", "Chase item was not found")
            session.expunge(row[0])
            session.expunge(row[1])
            return row[0], row[1]

    @router.post("/items/{item_id}/attest")
    def attest(
        item_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        role(x_actor)
        checklist, detached = item_context(item_id)
        if not detached.physical:
            raise ClaimCoreError(
                422,
                "CHASE_ITEM_NOT_PHYSICAL",
                "Only physical checklist items can be attested",
            )
        field_path = ATTESTED_FIELDS.get(detached.item_id)
        if field_path is not None and detached.state != "received":
            _claim, fields, _blocked = app.state.claim_service.hydrate_claim(
                checklist.claim_id, x_actor, paths=[field_path]
            )
            current = fields.get(field_path)
            if current is None or current.value is not True:
                app.state.claim_service.write_fields(
                    checklist.claim_id,
                    [
                        FieldWrite(
                            path=field_path,
                            value=True,
                            value_type="bool",
                            source_type="human",
                            source_ref={"user_id": x_actor, "chase_item_id": item_id},
                            verification_state="human_verified",
                        )
                    ],
                    x_actor,
                )
        with service.sessions.begin() as session:
            item = session.get(ChaseItem, item_id)
            current_checklist = session.get(ChaseChecklist, detached.checklist_id)
            if item is None or current_checklist is None:
                raise ClaimCoreError(404, "CHASE_ITEM_NOT_FOUND", "Chase item was not found")
            if item.state != "received":
                item.state = "received"
                item.received_at = aware(app.state.clock())
                service._emit(
                    session,
                    claim_id=current_checklist.claim_id,
                    event_type="chase.item_received",
                    payload={
                        **service.event_payload(current_checklist, item),
                        "attested_by": x_actor,
                        "physical": True,
                    },
                    actor=x_actor,
                )
                service.maybe_complete(session, current_checklist)
        return {"item_id": item_id, "state": "received"}

    @router.post("/items/{item_id}/waive")
    def waive(
        item_id: str,
        body: WaiveRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        actor_role = role(x_actor)
        checklist, detached = item_context(item_id)
        if checklist.blocking and actor_role != "claims_manager":
            raise ClaimCoreError(
                403,
                "WAIVER_REQUIRES_CLAIMS_MANAGER",
                "Blocking checklist waivers require claims_manager",
            )
        with service.sessions.begin() as session:
            item = session.get(ChaseItem, item_id)
            current_checklist = session.get(ChaseChecklist, detached.checklist_id)
            if item is None or current_checklist is None:
                raise ClaimCoreError(404, "CHASE_ITEM_NOT_FOUND", "Chase item was not found")
            if item.state != "waived":
                item.state = "waived"
                item.waived_by = x_actor
                item.waiver_reason = body.reason
                service._emit(
                    session,
                    claim_id=current_checklist.claim_id,
                    event_type="chase.item_waived",
                    payload={
                        **service.event_payload(current_checklist, item),
                        "waived_by": x_actor,
                        "reason": body.reason,
                    },
                    actor=x_actor,
                )
                service.maybe_complete(session, current_checklist)
        return {"item_id": item_id, "state": "waived"}

    @router.post("/items/{item_id}/snooze")
    def snooze(
        item_id: str,
        body: SnoozeRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        role(x_actor)
        checklist, detached = item_context(item_id)
        until = aware(body.until)
        with service.sessions.begin() as session:
            item = session.get(ChaseItem, item_id)
            current_checklist = session.get(ChaseChecklist, detached.checklist_id)
            if item is None or current_checklist is None:
                raise ClaimCoreError(404, "CHASE_ITEM_NOT_FOUND", "Chase item was not found")
            item.snooze_until = until
            service._emit(
                session,
                claim_id=checklist.claim_id,
                event_type="chase.item_snoozed",
                payload={
                    **service.event_payload(current_checklist, item),
                    "snooze_until": until.isoformat(),
                    "snoozed_by": x_actor,
                },
                actor=x_actor,
            )
        return {"item_id": item_id, "snooze_until": until}

    @router.get("/claims/{claim_id}")
    def read_claim(
        claim_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        role(x_actor)
        with service.sessions() as session:
            checklists = list(
                session.scalars(
                    select(ChaseChecklist)
                    .where(ChaseChecklist.claim_id == claim_id)
                    .order_by(ChaseChecklist.created_at, ChaseChecklist.id)
                )
            )
            response = []
            for checklist in checklists:
                items = list(
                    session.scalars(
                        select(ChaseItem)
                        .where(ChaseItem.checklist_id == checklist.id)
                        .order_by(ChaseItem.item_id, ChaseItem.id)
                    )
                )
                response.append(
                    {
                        "id": checklist.id,
                        "purpose": checklist.purpose,
                        "status": checklist.status,
                        "blocking": checklist.blocking,
                        "items": [
                            {
                                "id": item.id,
                                "item_id": item.item_id,
                                "state": item.state,
                                "physical": item.physical,
                                "requested_at": item.requested_at,
                                "received_at": item.received_at,
                                "verified_at": item.verified_at,
                                "waived_by": item.waived_by,
                                "waiver_reason": item.waiver_reason,
                                "reminder_count": item.reminder_count,
                                "next_reminder_at": item.next_reminder_at,
                                "document_id": item.document_id,
                                "reject_reason": item.reject_reason,
                                "snooze_until": item.snooze_until,
                            }
                            for item in items
                        ],
                    }
                )
        return {"checklists": response}

    return router


__all__ = ["build_router"]
