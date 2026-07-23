"""Trusted PACKET-12 console operations routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from claim_core import ClaimCoreError, Notification, new_ulid
from eval_harness import PromotionDenied
from review_queue.ops_reads import OpsReadService

SLA_VIEW_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "gm",
        "md",
        "chairman",
        "head_of_claims",
        "auditor",
    }
)
SLA_ESCALATE_ROLES = frozenset(
    {"asst_claims_manager", "claims_manager", "gm", "md", "chairman"}
)
PORTFOLIO_ROLES = frozenset(
    {"claims_manager", "gm", "md", "chairman", "head_of_claims", "auditor"}
)
PACK_READ_ROLES = frozenset({"admin", "auditor"})
CAPABILITY_READ_ROLES = frozenset({"admin", "auditor", "claims_manager"})
CAPABILITY_WRITE_ROLES = frozenset({"admin", "claims_manager"})
LEDGER_READ_ROLES = frozenset({"admin", "auditor", "claims_manager"})


class EscalateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clock_ids: list[str] = Field(min_length=1)


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_level: int | str
    sign_offs: list[dict[str, str]] = Field(default_factory=list)


def _role(app: Any, actor: str) -> str:
    role = app.state.review_queue.service.authorizer.role(actor)
    if role is None:
        raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Actor has no configured role")
    return role


def _require(app: Any, actor: str, allowed: frozenset[str]) -> str:
    role = _role(app, actor)
    if role not in allowed:
        raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Role is not authorised for this surface")
    return role


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def build_ops_router(app: Any) -> APIRouter:
    router = APIRouter(prefix="/console/ops", tags=["console-ops"])
    reads = OpsReadService(app)

    @router.get("/notifications")
    def notifications(
        scope: str = Query(default="mine"),
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        _role(app, x_actor)
        if scope != "mine":
            raise ClaimCoreError(422, "VALUE_TYPE_MISMATCH", "scope must be mine")
        with Session(app.state.engine) as session:
            rows = list(
                session.scalars(
                    select(Notification)
                    .where(Notification.recipient == x_actor)
                    .order_by(Notification.created_at.desc(), Notification.id.desc())
                )
            )
        return {
            "items": [
                {
                    "id": row.id,
                    "recipient": row.recipient,
                    "rule_id": row.rule_id,
                    "event_id": row.event_id,
                    "claim_id": row.claim_id,
                    "channel": row.channel,
                    "status": row.status,
                    "payload": row.payload,
                    "created_at": _iso(row.created_at),
                    "read_at": _iso(row.read_at),
                }
                for row in rows
            ]
        }

    @router.post("/notifications/{notification_id}/read")
    def mark_read(
        notification_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, str]:
        if _role(app, x_actor) == "auditor":
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Auditor access is read-only")
        with Session(app.state.engine) as session, session.begin():
            row = session.get(Notification, notification_id)
            if row is None or row.recipient != x_actor:
                raise ClaimCoreError(404, "NOTIFICATION_NOT_FOUND", "Notification was not found")
            row.status = "read"
            row.read_at = app.state.clock()
        return {"status": "read"}

    @router.get("/sla-board")
    def sla_board(x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        _require(app, x_actor, SLA_VIEW_ROLES)
        return {"clocks": reads.sla_board()}

    @router.post("/sla-board/escalate")
    def escalate(
        body: EscalateRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        _require(app, x_actor, SLA_ESCALATE_ROLES)
        clocks = {row["clock_id"]: row for row in reads.sla_board()}
        results = []
        for clock_id in body.clock_ids:
            clock = clocks.get(clock_id)
            if clock is None:
                results.append(
                    {
                        "clock_id": clock_id,
                        "outcome": "blocked_on_inputs",
                        "blocked_on": "clock_not_open",
                    }
                )
                continue
            target = clock["escalate_to_role"]
            if target == "pending_capture" or not any(
                role == target for role in app.state.review_queue.roles.values()
            ):
                results.append(
                    {
                        "clock_id": clock_id,
                        "outcome": "blocked_on_inputs",
                        "blocked_on": "escalate_to_role",
                    }
                )
                continue
            with Session(app.state.engine) as session, session.begin():
                app.state.record_event(
                    session,
                    claim_id=clock["claim_id"],
                    event_type="sla.escalated",
                    payload={
                        "clock_id": clock_id,
                        "definition_id": clock["definition_id"],
                        "escalate_to_role": target,
                    },
                    actor=x_actor,
                    correlation_id=new_ulid(),
                )
            results.append(
                {"clock_id": clock_id, "outcome": "escalated", "role": target}
            )
        return {"results": results}

    @router.get("/portfolio")
    def portfolio(x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        _require(app, x_actor, PORTFOLIO_ROLES)
        return {"tiles": reads.portfolio()}

    @router.get("/portfolio/{series_id}.csv")
    def portfolio_csv(
        series_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> Response:
        _require(app, x_actor, PORTFOLIO_ROLES)
        return Response(
            content=reads.csv(series_id),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{series_id}.csv"',
                "Cache-Control": "private, no-store",
            },
        )

    @router.get("/ledger")
    def ledger(
        actor: str | None = None,
        action: str | None = None,
        claim_id: str | None = None,
        after_seq: int | None = Query(default=None, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        _require(app, x_actor, LEDGER_READ_ROLES)
        return {
            "rows": reads.ledger(
                actor=actor,
                action=action,
                claim_id=claim_id,
                after_seq=after_seq,
                limit=limit,
            )
        }

    @router.get("/packs")
    def packs(x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        _require(app, x_actor, PACK_READ_ROLES)
        return reads.packs()

    @router.post("/projection-circuits/{operation_id}/clear")
    def clear_projection_circuit(
        operation_id: str,
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        # PACKET-21 §11: only admin or claims-manager may clear, and only after
        # a strictly newer definition version passes and the adapter is healthy.
        _require(app, x_actor, CAPABILITY_WRITE_ROLES)
        return reads.clear_projection_circuit(operation_id, actor=x_actor)

    @router.get("/capabilities")
    def capabilities(x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        _require(app, x_actor, CAPABILITY_READ_ROLES)
        return {"capabilities": reads.capabilities()}

    @router.post("/capabilities/{capability_id}/promote", response_model=None)
    def promote(
        capability_id: str,
        body: PromoteRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> Any:
        _require(app, x_actor, CAPABILITY_WRITE_ROLES)
        checked_sign_offs = []
        for sign_off in body.sign_offs:
            sign_actor = sign_off.get("actor")
            claimed_role = sign_off.get("role")
            actual_role = app.state.review_queue.service.authorizer.role(sign_actor or "")
            if actual_role is None or actual_role != claimed_role:
                raise ClaimCoreError(
                    403, "FORBIDDEN_ROLE", "A promotion sign-off role is not verified"
                )
            checked_sign_offs.append(dict(sign_off))
        to_level = (
            f"L{body.to_level}" if isinstance(body.to_level, int) else body.to_level
        )
        try:
            return app.state.eval_harness.autonomy.request_promotion(
                capability_id,
                to_level,
                sign_offs=checked_sign_offs,
                actor=x_actor,
            )
        except PromotionDenied as error:
            status = 409 if error.code == "CRITERIA_NOT_MET" else 422
            raise ClaimCoreError(status, error.code, "Capability promotion was denied") from error

    return router


__all__ = ["build_ops_router"]
