"""Checklist instantiation and lifecycle consumers for PRD-06."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from chase_agent.models import ChaseChecklist, ChaseItem
from claim_core import new_ulid

ACTOR = "agent:chase"
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
    ) -> None:
        with self.sessions.begin() as session:
            rows = session.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for raw in rows:
                current = raw if isinstance(raw, dict) else {}
                if current.get("subtype") == subtype and all(
                    current.get(key) == value for key, value in identity.items()
                ):
                    return
            self._emit(
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

    def _request_exists(self, claim_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for payload in rows:
                if not isinstance(payload, dict):
                    continue
                if payload.get("capability_id") == "intake.doc_request" and (
                    payload.get("action", {}).get("payload", {}).get("template_id")
                    == "T-06"
                ):
                    return True
        return False

    def ensure_initial_request(
        self,
        checklist_id: str,
        claim_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        if self._request_exists(claim_id):
            return "existing"
        requester_id, _tone = self.requester(claim_id)
        if requester_id is None:
            self._exception_once(
                claim_id=claim_id,
                subtype="chase_requester_missing",
                identity={"checklist_id": checklist_id},
                payload={"items": []},
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
            action_payload=summary,
        )
        if outcome["status"] not in {"staged", "executed"}:
            return str(outcome["status"])
        next_at = requested_at + timedelta(days=int(self.config["cadence_days"][0]))
        with self.sessions.begin() as session:
            checklist = session.get(ChaseChecklist, checklist_id)
            if checklist is None or checklist.status != "open":
                return "not_needed"
            items = session.scalars(
                select(ChaseItem)
                .where(
                    ChaseItem.checklist_id == checklist_id,
                    ChaseItem.state == "pending",
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
                checklist_id = existing.id
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
                checklist_id = checklist.id
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
        self.ensure_initial_request(checklist_id, claim_id)

    def _instantiate_surrender(self, event: Any) -> None:
        claim_id = event.claim_id
        if not isinstance(claim_id, str):
            return
        with self.sessions() as session:
            existing = session.scalar(
                select(ChaseChecklist.id).where(
                    ChaseChecklist.claim_id == claim_id,
                    ChaseChecklist.purpose == "surrender",
                )
            )
        if existing is not None:
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
        if event.type != "claim.status_changed" or not isinstance(event.payload, dict):
            return
        target = event.payload.get("to")
        if target in SUPPRESSED_STATES and isinstance(event.claim_id, str):
            self.cancel_claim(event.claim_id, correlation_id=event.id)
        elif target == "SURRENDER_CHECKLIST":
            self._instantiate_surrender(event)


__all__ = [
    "ACTOR",
    "ChecklistService",
    "OUTSTANDING_STATES",
    "SUPPRESSED_STATES",
    "aware",
]
