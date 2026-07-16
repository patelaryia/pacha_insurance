"""Clock-driven, pack-configured PRD-06 reminder selection."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text

from chase_agent.checklist import (
    ACTOR,
    OUTSTANDING_STATES,
    SUPPRESSED_STATES,
    ChecklistService,
    aware,
)
from chase_agent.models import ChaseChecklist, ChaseItem
from claim_core import new_ulid


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


class ReminderEngine:
    """Select and govern at most one reminder per due checklist."""

    def __init__(self, app: Any, checklist: ChecklistService, config: dict[str, Any]) -> None:
        self.app = app
        self.checklist = checklist
        self.config = config

    def _inbound_reply(self, claim_id: str, now: Any) -> Any | None:
        threshold = now - timedelta(
            hours=int(self.config["inbound_defer"]["window_hours"])
        )
        with self.app.state.engine.connect() as connection:
            values = list(
                connection.execute(
                    text(
                        "SELECT occurred_at FROM communications "
                        "WHERE claim_id = :claim_id AND direction = 'inbound' "
                        "ORDER BY occurred_at DESC, id DESC"
                    ),
                    {"claim_id": claim_id},
                ).scalars()
            )
        for value in values:
            occurred = aware(value)
            if threshold <= occurred <= now:
                return occurred
        return None

    def _already_escalated(self, claim_id: str, checklist_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for raw in rows:
                payload = _payload(raw)
                if (
                    payload.get("subtype") == "chase_exhausted"
                    and payload.get("checklist_id") == checklist_id
                ):
                    return True
        return False

    def _escalate(
        self, checklist: ChaseChecklist, items: list[ChaseItem]
    ) -> bool:
        if self._already_escalated(checklist.claim_id, checklist.id):
            return False
        with self.checklist.sessions.begin() as session:
            self.checklist._emit(
                session,
                claim_id=checklist.claim_id,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "EXCEPTION",
                    "subtype": "chase_exhausted",
                    "checklist_id": checklist.id,
                    "items": sorted(item.item_id for item in items),
                    "role": "claims_officer",
                },
            )
        return True

    def _next_reminder(self, item: ChaseItem, new_count: int) -> Any:
        if item.requested_at is None:
            raise ValueError("requested chase item has no requested_at")
        cadence = [int(value) for value in self.config["cadence_days"]]
        if new_count < len(cadence):
            days = cadence[new_count]
        else:
            days = cadence[-1] + int(self.config["repeat_days"]) * (
                new_count - len(cadence) + 1
            )
        return aware(item.requested_at) + timedelta(days=days)

    def tick(self, now: Any = None) -> dict[str, int]:
        evaluated_at = aware(now or self.app.state.clock())
        result = {"sent": 0, "deferred": 0, "escalated": 0, "suppressed": 0}
        with self.checklist.sessions() as session:
            unrequested = session.execute(
                select(ChaseChecklist.id, ChaseChecklist.claim_id)
                .join(ChaseItem, ChaseItem.checklist_id == ChaseChecklist.id)
                .where(
                    ChaseChecklist.purpose == "claim_docs",
                    ChaseChecklist.status == "open",
                    ChaseItem.state == "pending",
                    ChaseItem.requested_at.is_(None),
                )
                .distinct()
                .order_by(ChaseChecklist.id)
            ).all()
        for checklist_id, claim_id in unrequested:
            with self.app.state.engine.connect() as connection:
                status = connection.execute(
                    text("SELECT status FROM claims WHERE id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalar()
            if status in SUPPRESSED_STATES:
                result["suppressed"] += self.checklist.cancel_claim(str(claim_id))
                continue
            self.checklist.ensure_initial_request(
                str(checklist_id), str(claim_id), now=evaluated_at
            )
        with self.checklist.sessions() as session:
            checklist_ids = list(
                session.scalars(
                    select(ChaseChecklist.id)
                    .where(ChaseChecklist.status == "open")
                    .order_by(ChaseChecklist.created_at, ChaseChecklist.id)
                )
            )
        for checklist_id in checklist_ids:
            with self.checklist.sessions() as session:
                checklist = session.get(ChaseChecklist, checklist_id)
                if checklist is None or checklist.status != "open":
                    continue
                claim_id = checklist.claim_id
                status = session.execute(
                    text("SELECT status FROM claims WHERE id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalar()
                due = list(
                    session.scalars(
                        select(ChaseItem)
                        .where(
                            ChaseItem.checklist_id == checklist_id,
                            ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                            ChaseItem.next_reminder_at.is_not(None),
                            ChaseItem.next_reminder_at <= evaluated_at,
                        )
                        .order_by(ChaseItem.item_id, ChaseItem.id)
                    )
                )
                due = [
                    item
                    for item in due
                    if item.snooze_until is None
                    or aware(item.snooze_until) <= evaluated_at
                ]
                if status in SUPPRESSED_STATES:
                    detached_claim = claim_id
                else:
                    detached_claim = None
                due_ids = [item.id for item in due]
                capped = [
                    item
                    for item in due
                    if item.reminder_count >= int(self.config["reminder_cap"])
                ]
                sendable = [item for item in due if item not in capped]
            if detached_claim is not None:
                result["suppressed"] += self.checklist.cancel_claim(detached_claim)
                continue
            if not due_ids:
                continue
            inbound = self._inbound_reply(claim_id, evaluated_at)
            if inbound is not None:
                defer_until = inbound + timedelta(
                    hours=int(self.config["inbound_defer"]["defer_hours"])
                )
                deferred_count = 0
                with self.checklist.sessions.begin() as session:
                    outstanding = session.scalars(
                        select(ChaseItem).where(
                            ChaseItem.checklist_id == checklist_id,
                            ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                            ChaseItem.next_reminder_at.is_not(None),
                        )
                    )
                    for item in outstanding:
                        if aware(item.next_reminder_at) < defer_until:
                            item.next_reminder_at = defer_until
                            deferred_count += 1
                result["deferred"] += deferred_count
                continue
            if capped:
                with self.checklist.sessions() as session:
                    current = session.get(ChaseChecklist, checklist_id)
                    if current is not None and self._escalate(current, capped):
                        result["escalated"] += 1
            if not sendable:
                continue
            requester, tone = self.checklist.requester(claim_id)
            if requester is None:
                self.checklist._exception_once(
                    claim_id=claim_id,
                    subtype="chase_requester_missing",
                    identity={"checklist_id": checklist_id},
                    payload={"items": [item.item_id for item in sendable]},
                )
                continue
            recipients = [requester]
            next_index = max(item.reminder_count for item in sendable) + 1
            if next_index >= int(self.config["cc_insured_from_reminder"]):
                insured = self.checklist.insured_party(claim_id)
                if insured is not None and insured not in recipients:
                    recipients.append(insured)
            context = self.checklist.summary_payload(
                checklist_id, now=evaluated_at, include_snoozed=False
            )
            outcome = self.app.state.agent_runtime.comms.send(
                template_id=f"T-06r-{tone}",
                claim_id=claim_id,
                to_party_ids=recipients,
                attachments=(),
                capability_id="chase.reminder",
                actor=ACTOR,
                action_payload=context,
            )
            if outcome["status"] not in {"staged", "executed"}:
                continue
            result["sent"] += 1
            with self.checklist.sessions.begin() as session:
                current_checklist = session.get(ChaseChecklist, checklist_id)
                if current_checklist is None or current_checklist.status != "open":
                    continue
                for detached in sendable:
                    item = session.get(ChaseItem, detached.id)
                    if item is None or item.state not in OUTSTANDING_STATES:
                        continue
                    item.reminder_count += 1
                    item.next_reminder_at = self._next_reminder(
                        item, item.reminder_count
                    )
                if outcome["status"] == "executed":
                    self.checklist._emit(
                        session,
                        claim_id=claim_id,
                        event_type="chase.reminder_sent",
                        payload={
                            "claim_id": claim_id,
                            "checklist_id": checklist_id,
                            "items": [item.item_id for item in sendable],
                        },
                    )
        return result


__all__ = ["ReminderEngine"]
