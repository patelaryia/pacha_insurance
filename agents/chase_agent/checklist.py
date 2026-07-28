"""Checklist instantiation and lifecycle consumers for PRD-06."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from chase_agent.models import ChaseChecklist, ChaseItem
from claim_core import new_ulid
from orchestration.ids import chase_workflow_ref

ACTOR = "agent:chase"
CHASE_CAPABILITY_ID = "chase.checklist"
CHASE_WORKFLOW_TYPE = "DocumentChaseWorkflow"
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
FINAL_ITEM_STATES = frozenset({"verified", "waived"})
OUTSTANDING_STATES = frozenset({"pending", "requested", "rejected"})
SUPPRESSED_STATES = frozenset({"DECLINED", "WITHDRAWN", "VOID", "SETTLED", "CLOSED"})


def aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ChecklistService:
    """Own checklist rows and the events emitted for every state advance."""

    def __init__(
        self,
        app: Any,
        registry: dict[str, dict[str, Any]],
        config: dict[str, Any],
    ) -> None:
        self.app = app
        self.registry = registry
        self.config = config
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _prepare_workflow(
        self,
        session: Session,
        *,
        checklist: ChaseChecklist,
        trigger_event_ref: str,
    ) -> str:
        """Atomically prepare the run projection and its Temporal start intent."""

        workflow_ref = chase_workflow_ref(checklist.id)
        existing = session.execute(
            text("SELECT id FROM agent_runs WHERE workflow_id = :workflow_id"),
            {"workflow_id": str(workflow_ref)},
        ).scalar()
        if isinstance(existing, str):
            return existing

        run_ref = new_ulid()
        runner = self.app.state.agent_runtime.runner
        step_ids = runner.definitions.get(CHASE_CAPABILITY_ID)
        if step_ids is None:
            raise ValueError("chase.checklist COP steps are not registered")
        self.app.state.agent_runtime.projection.prepare(
            session,
            run_ref=run_ref,
            agent="chase",
            capability_id=CHASE_CAPABILITY_ID,
            autonomy_level=runner.level(CHASE_CAPABILITY_ID),
            workflow_ref=workflow_ref,
            workflow_type=CHASE_WORKFLOW_TYPE,
            claim_ref=checklist.claim_id,
            trigger_event_ref=trigger_event_ref,
            step_ids=step_ids,
        )
        self._emit(
            session,
            claim_id=checklist.claim_id,
            event_type="chase.workflow_requested",
            payload={
                "claim_id": checklist.claim_id,
                "checklist_id": checklist.id,
                "purpose": checklist.purpose,
            },
            correlation_id=run_ref,
        )
        return run_ref

    def _emit(
        self,
        session: Session,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str = ACTOR,
        correlation_id: str | None = None,
    ) -> str:
        event = self.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
            correlation_id=correlation_id or new_ulid(),
        )
        return event.id

    def emit_event(
        self,
        session: Session,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str = ACTOR,
        correlation_id: str | None = None,
    ) -> str:
        """Public event boundary used by chase Activities."""

        return self._emit(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
            correlation_id=correlation_id,
        )

    @staticmethod
    def event_payload(checklist: ChaseChecklist, item: ChaseItem) -> dict[str, Any]:
        return {
            "claim_id": checklist.claim_id,
            "checklist_id": checklist.id,
            "chase_item_id": item.id,
            "item_id": item.item_id,
        }

    def _exception_once(
        self,
        *,
        claim_id: str,
        subtype: str,
        identity: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        with self.sessions.begin() as session:
            rows = session.execute(
                text(
                    "SELECT id, payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).all()
            for event_ref, raw in rows:
                current = raw if isinstance(raw, dict) else {}
                if current.get("subtype") == subtype and all(
                    current.get(key) == value for key, value in identity.items()
                ):
                    return str(event_ref)
            return self._emit(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "EXCEPTION",
                    "subtype": subtype,
                    **identity,
                    **payload,
                },
            )

    def exception_once(
        self,
        *,
        claim_id: str,
        subtype: str,
        identity: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Create or resolve one claim-scoped chase exception event."""

        return self._exception_once(
            claim_id=claim_id,
            subtype=subtype,
            identity=identity,
            payload=payload,
        )

    def authorise_wake_actor(self, event_type: str, actor: str) -> bool:
        """Revalidate authority for a human-originated chase wake event."""

        if actor == ACTOR:
            return True
        if event_type not in {
            "chase.item_received",
            "chase.item_waived",
            "chase.item_snoozed",
        }:
            return False
        return (
            self.app.state.review_queue.service.authorizer.role(actor)
            in CHASE_ROLES
        )

    def _held_document_id(self, claim_id: str, doc_type: str) -> str | None:
        with self.app.state.engine.connect() as connection:
            values = list(
                connection.execute(
                    text(
                        "SELECT id FROM documents WHERE claim_id = :claim_id "
                        "AND doc_type = :doc_type ORDER BY received_at, id"
                    ),
                    {"claim_id": claim_id, "doc_type": doc_type},
                ).scalars()
            )
        return str(values[0]) if len(values) == 1 else None

    def requester(
        self,
        claim_id: str,
        requester_party_id: str | None = None,
    ) -> tuple[str | None, str]:
        if requester_party_id is not None:
            with self.app.state.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT id, role FROM parties WHERE id = :party_id "
                        "AND claim_id = :claim_id"
                    ),
                    {"party_id": requester_party_id, "claim_id": claim_id},
                ).first()
            if row is None or str(row[1]) != "assessor":
                return None, "assessor"
            return str(row[0]), "assessor"
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, role, meta FROM parties WHERE claim_id = :claim_id "
                    "AND role IN ('broker', 'insured', 'agent') "
                    "ORDER BY id"
                ),
                {"claim_id": claim_id},
            ).all()
        senders = []
        for row in rows:
            meta = row[2]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            if isinstance(meta, dict) and meta.get("source") == "intimation_sender":
                senders.append(row)
        if len(senders) != 1:
            return None, "client"
        row = senders[0]
        return str(row[0]), "broker" if str(row[1]) in {"broker", "agent"} else "client"

    def create_assessor_report(
        self,
        *,
        claim_id: str,
        requester_party_id: str,
        correlation_id: str,
        now: datetime | None = None,
    ) -> str:
        """Create the one-item PRD-07 checklist already requested by T-11."""

        requested_at = aware(now or self.app.state.clock())
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(ChaseChecklist)
                .where(
                    ChaseChecklist.claim_id == claim_id,
                    ChaseChecklist.purpose == "assessor_report",
                    ChaseChecklist.requester_party_id == requester_party_id,
                )
                .order_by(ChaseChecklist.created_at, ChaseChecklist.id)
                .limit(1)
            )
            if existing is not None:
                self._prepare_workflow(
                    session,
                    checklist=existing,
                    trigger_event_ref=correlation_id,
                )
                return existing.id
            definition = self.registry["assessor_report"]
            checklist = ChaseChecklist(
                id=new_ulid(),
                claim_id=claim_id,
                purpose="assessor_report",
                status="open",
                blocking=False,
                requester_party_id=requester_party_id,
                created_at=requested_at,
            )
            session.add(checklist)
            session.flush()
            self._prepare_workflow(
                session,
                checklist=checklist,
                trigger_event_ref=correlation_id,
            )
            item = ChaseItem(
                id=new_ulid(),
                checklist_id=checklist.id,
                item_id="assessor_report",
                state="requested",
                physical=bool(definition["physical"]),
                requested_at=requested_at,
                reminder_count=0,
                next_reminder_at=requested_at
                + timedelta(days=int(self.config["cadence_days"][0])),
            )
            session.add(item)
            session.flush()
            self._emit(
                session,
                claim_id=claim_id,
                event_type="chase.item_requested",
                payload=self.event_payload(checklist, item),
                correlation_id=correlation_id,
            )
            return checklist.id

    def insured_party(self, claim_id: str) -> str | None:
        with self.app.state.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT id FROM parties WHERE claim_id = :claim_id "
                    "AND role = 'insured' ORDER BY id LIMIT 1"
                ),
                {"claim_id": claim_id},
            ).scalar()
        return str(value) if isinstance(value, str) else None

    def summary_payload(
        self,
        checklist_id: str,
        *,
        now: datetime,
        include_snoozed: bool = True,
    ) -> dict[str, Any]:
        with self.sessions() as session:
            items = list(
                session.scalars(
                    select(ChaseItem)
                    .where(ChaseItem.checklist_id == checklist_id)
                    .order_by(ChaseItem.item_id, ChaseItem.id)
                )
            )
        outstanding = []
        received = []
        for item in items:
            snoozed = item.snooze_until is not None and aware(item.snooze_until) > now
            if item.state in OUTSTANDING_STATES and (include_snoozed or not snoozed):
                requested = aware(item.requested_at) if item.requested_at is not None else now
                outstanding.append(
                    {
                        "item_id": item.item_id,
                        "age_days": max(0, (now - requested).days),
                    }
                )
            elif item.state in {"received", "verified"}:
                received.append(item.item_id)
        return {"outstanding": outstanding, "received": sorted(received)}

    def _existing_request_at(self, claim_id: str) -> datetime | None:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT occurred_at, payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).all()
            for occurred_at, payload in rows:
                if not isinstance(payload, dict):
                    continue
                if payload.get("capability_id") == "intake.doc_request" and (
                    payload.get("action", {}).get("payload", {}).get("template_id")
                    == "T-06"
                ):
                    return aware(occurred_at)
        return None

    def _mark_initial_requested(
        self,
        checklist_id: str,
        claim_id: str,
        *,
        requested_at: datetime,
    ) -> None:
        next_at = requested_at + timedelta(days=int(self.config["cadence_days"][0]))
        with self.sessions.begin() as session:
            checklist = session.get(ChaseChecklist, checklist_id)
            if checklist is None or checklist.status != "open":
                return
            items = session.scalars(
                select(ChaseItem)
                .where(
                    ChaseItem.checklist_id == checklist_id,
                    ChaseItem.state == "pending",
                    ChaseItem.requested_at.is_(None),
                )
                .order_by(ChaseItem.item_id, ChaseItem.id)
            )
            for item in items:
                item.state = "requested"
                item.requested_at = requested_at
                item.next_reminder_at = next_at
                self._emit(
                    session,
                    claim_id=claim_id,
                    event_type="chase.item_requested",
                    payload=self.event_payload(checklist, item),
                )

    def ensure_initial_request(
        self,
        checklist_id: str,
        claim_id: str,
        *,
        now: datetime | None = None,
        run_id: str | None = None,
        authorisation_event_ref: str | None = None,
    ) -> str:
        existing_at = self._existing_request_at(claim_id)
        if existing_at is not None:
            self._mark_initial_requested(
                checklist_id,
                claim_id,
                requested_at=existing_at,
            )
            return "existing"
        requester_id, _tone = self.requester(claim_id)
        if requester_id is None:
            identity: dict[str, Any] = {"checklist_id": checklist_id}
            if authorisation_event_ref is not None:
                identity["authorised_by"] = authorisation_event_ref
            self._exception_once(
                claim_id=claim_id,
                subtype="chase_requester_missing",
                identity=identity,
                payload={
                    "facts": {"items": []},
                    "risk": "the document request has no uniquely captured requester",
                    "recommendation": "capture the requester before authorising another attempt",
                    "resolution_schema": "EXCEPTION@1",
                    "role": self.config["exception_routing_role"],
                },
            )
            return "refused"
        requested_at = aware(now or self.app.state.clock())
        summary = self.summary_payload(checklist_id, now=requested_at)
        if not summary["outstanding"]:
            return "not_needed"
        outcome = self.app.state.agent_runtime.comms.send(
            template_id="T-06",
            claim_id=claim_id,
            to_party_ids=[requester_id],
            attachments=(),
            capability_id="intake.doc_request",
            actor=ACTOR,
            run_id=run_id,
            action_payload=summary,
        )
        if outcome["status"] not in {"staged", "executed"}:
            return str(outcome["status"])
        self._mark_initial_requested(
            checklist_id,
            claim_id,
            requested_at=requested_at,
        )
        return str(outcome["status"])

    def _instantiate_claim_docs(self, event: Any) -> None:
        claim_id = event.claim_id or event.payload.get("claim_id")
        if not isinstance(claim_id, str):
            return
        raw_items = event.payload.get("items")
        if not isinstance(raw_items, list):
            self._exception_once(
                claim_id=claim_id,
                subtype="chase_init_invalid",
                identity={"source_event_id": event.id},
                payload={"reason": "items_missing"},
            )
            return
        requested_ids = [row.get("id") for row in raw_items if isinstance(row, dict)]
        if (
            len(requested_ids) != len(raw_items)
            or len(requested_ids) != len(set(requested_ids))
            or any(
                not isinstance(item_id, str) or item_id not in self.registry
                for item_id in requested_ids
            )
        ):
            self._exception_once(
                claim_id=claim_id,
                subtype="chase_init_invalid",
                identity={"source_event_id": event.id},
                payload={"reason": "item_not_registered"},
            )
            return
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(ChaseChecklist)
                .where(
                    ChaseChecklist.claim_id == claim_id,
                    ChaseChecklist.purpose == "claim_docs",
                )
                .order_by(ChaseChecklist.created_at, ChaseChecklist.id)
                .limit(1)
            )
            if existing is not None:
                self._prepare_workflow(
                    session,
                    checklist=existing,
                    trigger_event_ref=event.id,
                )
            else:
                checklist = ChaseChecklist(
                    id=new_ulid(),
                    claim_id=claim_id,
                    purpose="claim_docs",
                    status="open",
                    blocking=False,
                    requester_party_id=None,
                    created_at=aware(event.occurred_at),
                )
                session.add(checklist)
                session.flush()
                # The start intent must precede any same-transaction item
                # Signals so the ordered outbox cannot Signal a missing
                # Workflow on first delivery.
                self._prepare_workflow(
                    session,
                    checklist=checklist,
                    trigger_event_ref=event.id,
                )
                for raw in raw_items:
                    item_id = str(raw["id"])
                    definition = self.registry[item_id]
                    already = raw.get("already_received") is True
                    document_id = None
                    if already and isinstance(definition.get("doc_type"), str):
                        document_id = self._held_document_id(
                            claim_id, str(definition["doc_type"])
                        )
                    state = "received" if already else "pending"
                    item = ChaseItem(
                        id=new_ulid(),
                        checklist_id=checklist.id,
                        item_id=item_id,
                        state=state,
                        physical=bool(definition["physical"]),
                        received_at=aware(event.occurred_at) if state == "received" else None,
                        document_id=document_id,
                        reminder_count=0,
                    )
                    session.add(item)
                    session.flush()
                    if state == "received":
                        self._emit(
                            session,
                            claim_id=claim_id,
                            event_type="chase.item_received",
                            payload=self.event_payload(checklist, item),
                            correlation_id=event.id,
                        )

    def _instantiate_surrender(self, event: Any) -> None:
        claim_id = event.claim_id
        if not isinstance(claim_id, str):
            return
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(ChaseChecklist).where(
                    ChaseChecklist.claim_id == claim_id,
                    ChaseChecklist.purpose == "surrender",
                )
            )
            if existing is not None:
                self._prepare_workflow(
                    session,
                    checklist=existing,
                    trigger_event_ref=event.id,
                )
                return
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["logbook.bank_interest.present"]
        )
        item_ids = ["logbook_original", "keys_physical", "kra_pin_cert"]
        bank_interest = fields.get("logbook.bank_interest.present")
        if bank_interest is not None and bank_interest.value is True:
            item_ids.append("bank_discharge_letter")
        now = aware(event.occurred_at)
        with self.sessions.begin() as session:
            checklist = ChaseChecklist(
                id=new_ulid(),
                claim_id=claim_id,
                purpose="surrender",
                status="open",
                blocking=True,
                requester_party_id=None,
                created_at=now,
            )
            session.add(checklist)
            session.flush()
            for item_id in item_ids:
                definition = self.registry[item_id]
                session.add(
                    ChaseItem(
                        id=new_ulid(),
                        checklist_id=checklist.id,
                        item_id=item_id,
                        state="pending",
                        physical=bool(definition["physical"]),
                        reminder_count=0,
                    )
                )
            self._prepare_workflow(
                session,
                checklist=checklist,
                trigger_event_ref=event.id,
            )

    def _emit_wake_events(
        self,
        *,
        source_event: Any,
        event_type: str,
        checklist_ids: list[str],
    ) -> None:
        if not isinstance(source_event.claim_id, str):
            return
        with self.sessions.begin() as session:
            for checklist_id in sorted(set(checklist_ids)):
                checklist = session.get(ChaseChecklist, checklist_id)
                if (
                    checklist is None
                    or checklist.claim_id != source_event.claim_id
                    or checklist.status != "open"
                ):
                    continue
                self._emit(
                    session,
                    claim_id=checklist.claim_id,
                    event_type=event_type,
                    payload={
                        "claim_id": checklist.claim_id,
                        "checklist_id": checklist.id,
                        "source_event_id": source_event.id,
                    },
                    correlation_id=source_event.id,
                )

    def _review_checklist_id(
        self,
        review_id: str,
        claim_id: str | None,
    ) -> str | None:
        with self.app.state.engine.connect() as connection:
            source_event_ref = connection.execute(
                text(
                    "SELECT source_event_id FROM review_items WHERE id = :review_id"
                ),
                {"review_id": review_id},
            ).scalar()
            if isinstance(source_event_ref, str):
                rows = connection.execute(
                    text(
                        "SELECT id, payload FROM events "
                        "WHERE id = :source_event_ref AND type = 'review.created'"
                    ),
                    {"source_event_ref": source_event_ref},
                ).all()
            else:
                rows = connection.execute(
                    text(
                        "SELECT id, payload FROM events "
                        "WHERE claim_id = :claim_id "
                        "AND type = 'review.created' ORDER BY seq"
                    ),
                    {"claim_id": claim_id},
                ).all()
            for event_ref, raw in rows:
                payload = raw
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(payload, dict):
                    continue
                if (
                    (
                        payload.get("review_id") == review_id
                        or event_ref == source_event_ref
                    )
                    and payload.get("type") == "EXCEPTION"
                    and isinstance(payload.get("checklist_id"), str)
                ):
                    return str(payload["checklist_id"])
        return None

    def cancel_claim(self, claim_id: str, *, correlation_id: str | None = None) -> int:
        cancelled = 0
        with self.sessions.begin() as session:
            checklists = session.scalars(
                select(ChaseChecklist)
                .where(
                    ChaseChecklist.claim_id == claim_id,
                    ChaseChecklist.status == "open",
                )
                .order_by(ChaseChecklist.created_at, ChaseChecklist.id)
            )
            for checklist in checklists:
                checklist.status = "cancelled"
                cancelled += 1
                self._emit(
                    session,
                    claim_id=claim_id,
                    event_type="chase.cancelled",
                    payload={
                        "claim_id": claim_id,
                        "checklist_id": checklist.id,
                        "purpose": checklist.purpose,
                    },
                    correlation_id=correlation_id,
                )
        return cancelled

    def maybe_complete(self, session: Session, checklist: ChaseChecklist) -> bool:
        if checklist.status != "open":
            return False
        items = list(
            session.scalars(
                select(ChaseItem).where(ChaseItem.checklist_id == checklist.id)
            )
        )
        if not items or not all(
            item.state in FINAL_ITEM_STATES
            or (item.physical and item.state == "received")
            for item in items
        ):
            return False
        checklist.status = "complete"
        self._emit(
            session,
            claim_id=checklist.claim_id,
            event_type="chase.complete",
            payload={
                "claim_id": checklist.claim_id,
                "checklist_id": checklist.id,
                "purpose": checklist.purpose,
            },
        )
        return True

    def consume(self, event: Any) -> None:
        if event.type == "chase.init" and isinstance(event.payload, dict):
            self._instantiate_claim_docs(event)
            return
        if event.type == "INBOUND_ATTACHED" and isinstance(event.claim_id, str):
            with self.sessions() as session:
                checklist_ids = list(
                    session.scalars(
                        select(ChaseChecklist.id).where(
                            ChaseChecklist.claim_id == event.claim_id,
                            ChaseChecklist.status == "open",
                        )
                    )
                )
            self._emit_wake_events(
                source_event=event,
                event_type="chase.inbound_received",
                checklist_ids=[str(value) for value in checklist_ids],
            )
            return
        if event.type == "review.resolved" and isinstance(event.payload, dict):
            review_id = event.payload.get("review_id")
            if not isinstance(review_id, str):
                return
            checklist_id = self._review_checklist_id(review_id, event.claim_id)
            if checklist_id is not None:
                self._emit_wake_events(
                    source_event=event,
                    event_type="chase.review_resolved",
                    checklist_ids=[checklist_id],
                )
            return
        if event.type != "claim.status_changed" or not isinstance(event.payload, dict):
            return
        target = event.payload.get("to")
        if target in SUPPRESSED_STATES and isinstance(event.claim_id, str):
            self.cancel_claim(event.claim_id, correlation_id=event.id)
        elif target == "SURRENDER_CHECKLIST":
            self._instantiate_surrender(event)


__all__ = [
    "ACTOR",
    "CHASE_CAPABILITY_ID",
    "CHASE_ROLES",
    "CHASE_WORKFLOW_TYPE",
    "ChecklistService",
    "OUTSTANDING_STATES",
    "SUPPRESSED_STATES",
    "aware",
]
