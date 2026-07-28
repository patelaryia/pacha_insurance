"""PRD-06 Activities behind ``DocumentChaseWorkflow``.

Temporal sees only :mod:`orchestration.contracts` values.  Recipient details,
checklist items, claim state, pack/template pins and review resolutions are
loaded from Pacha stores inside these Activities and never returned to Workflow
history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from temporalio import activity

from agent_runtime.models import AgentRun
from chase_agent.checklist import (
    ACTOR,
    OUTSTANDING_STATES,
    SUPPRESSED_STATES,
    ChecklistService,
    aware,
)
from chase_agent.models import ChaseChecklist, ChaseItem
from orchestration.contracts import ControlCommand, ControlResult
from orchestration.errors import sanitised_application_error
from orchestration.ids import parse_workflow_ref

__all__ = ["ChaseActivities", "chase_activity_registrations"]

LOGGER = logging.getLogger(__name__)

_STEP_START = "chase_record_start"
_STEP_LOAD = "chase_load_state"
_STEP_INITIAL = "chase_initial_request"
_STEP_WAIT = "chase_wait"
_STEP_APPLY = "chase_apply_event"
_STEP_REMINDER = "chase_reminder"
_STEP_EXHAUSTED = "chase_exhausted"
_STEP_TERMINAL = "chase_terminal"
_WAKE_EVENT_TYPES = frozenset(
    {
        "chase.item_requested",
        "chase.item_received",
        "chase.item_verified",
        "chase.item_rejected",
        "chase.item_waived",
        "chase.item_snoozed",
        "chase.reminder_sent",
        "chase.complete",
        "chase.cancelled",
        "chase.inbound_received",
        "chase.review_resolved",
    }
)
_CHASE_EXCEPTION_SUBTYPES = frozenset(
    {
        "chase_exhausted",
        "chase_requester_missing",
        "chase_send_refused",
        "uncertain_write",
    }
)
_CONTINUE_RESOLUTIONS = frozenset({"approved", "edited"})


@dataclass(frozen=True, slots=True)
class _ReviewState:
    event_ref: str
    subtype: str
    resolution: str | None
    resolution_event_ref: str | None
    write_id: str | None


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


class ChaseActivities:
    """Authoritative database/effect operations for one checklist Workflow."""

    def __init__(
        self,
        app: Any,
        checklist: ChecklistService,
        *,
        worker_build_id: str,
    ) -> None:
        if not hasattr(app.state, "agent_runtime"):
            raise RuntimeError("ChaseActivities requires app.state.agent_runtime")
        if not isinstance(checklist, ChecklistService):
            raise RuntimeError("ChaseActivities requires a ChecklistService")
        if not isinstance(worker_build_id, str) or not worker_build_id:
            raise RuntimeError("ChaseActivities requires the deployed Worker build id")
        self.app = app
        self.checklist = checklist
        self.config = checklist.config
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.projection = app.state.agent_runtime.projection
        self.worker_build_id = worker_build_id

    # -- control helpers -------------------------------------------------

    @staticmethod
    def _refs(command: ControlCommand) -> tuple[str, str]:
        if command.checklist_ref is None:
            raise ValueError("checklist_ref is required")
        return command.run_ref, command.checklist_ref

    def _step(
        self,
        run_ref: str,
        step_id: str,
        *,
        status: str,
        event_ref: str | None = None,
        event_seq: int | None = None,
        write_id: str | None = None,
    ) -> None:
        """Project one domain step without putting its detail in Temporal."""

        now = aware(self.app.state.clock())
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_ref)
            if run is None:
                raise LookupError("chase agent run was not found")
            steps = [dict(value) for value in run.steps]
            for value in steps:
                if value.get("step_id") != step_id:
                    continue
                value["status"] = status
                value["attempts"] = int(value.get("attempts", 0)) + 1
                value["updated_at"] = now.isoformat()
                if event_ref is not None:
                    value["event_ref"] = event_ref
                if event_seq is not None:
                    value["event_seq"] = event_seq
                if write_id is not None:
                    value["write_id"] = write_id
                run.steps = steps
                return
            raise ValueError(f"run does not declare step {step_id!r}")

    def _claim_status(self, claim_ref: str) -> str | None:
        with self.app.state.engine.connect() as connection:
            value = connection.execute(
                text("SELECT status FROM claims WHERE id = :claim_ref"),
                {"claim_ref": claim_ref},
            ).scalar()
        return str(value) if isinstance(value, str) else None

    def _applied_event_seq(self, run_ref: str) -> int:
        with self.sessions() as session:
            run = session.get(AgentRun, run_ref)
            if run is None:
                raise LookupError("chase agent run was not found")
            for value in run.steps:
                if value.get("step_id") != _STEP_APPLY:
                    continue
                event_seq = value.get("event_seq")
                return int(event_seq) if isinstance(event_seq, int) else 0
        return 0

    def _latest_checklist_event(
        self,
        claim_ref: str,
        checklist_ref: str,
    ) -> str | None:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, payload FROM events "
                    "WHERE claim_id = :claim_ref AND type LIKE 'chase.%' "
                    "ORDER BY seq DESC"
                ),
                {"claim_ref": claim_ref},
            ).all()
        for event_ref, raw in rows:
            if _payload(raw).get("checklist_id") == checklist_ref:
                return str(event_ref)
        return None

    def _latest_inbound(self, claim_ref: str, now: datetime) -> datetime | None:
        threshold = now - timedelta(
            hours=int(self.config["inbound_defer"]["window_hours"])
        )
        with self.app.state.engine.connect() as connection:
            values = connection.execute(
                text(
                    "SELECT occurred_at FROM communications "
                    "WHERE claim_id = :claim_ref AND direction = 'inbound' "
                    "ORDER BY occurred_at DESC, id DESC"
                ),
                {"claim_ref": claim_ref},
            ).scalars()
            for value in values:
                occurred = aware(value)
                if threshold <= occurred <= now:
                    return occurred
        return None

    def _persist_recent_deferral(
        self,
        checklist_ref: str,
        claim_ref: str,
        now: datetime,
    ) -> datetime | None:
        inbound = self._latest_inbound(claim_ref, now)
        if inbound is None:
            return None
        defer_until = inbound + timedelta(
            hours=int(self.config["inbound_defer"]["defer_hours"])
        )
        with self.sessions.begin() as session:
            items = session.scalars(
                select(ChaseItem).where(
                    ChaseItem.checklist_id == checklist_ref,
                    ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                    ChaseItem.next_reminder_at.is_not(None),
                )
            )
            for item in items:
                if aware(item.next_reminder_at) < defer_until:
                    item.next_reminder_at = defer_until
        return defer_until

    def _review_state(
        self,
        claim_ref: str,
        checklist_ref: str,
        *,
        subtype: str | None = None,
    ) -> _ReviewState | None:
        """Return the latest matching checklist exception and its resolution."""

        created: tuple[str, dict[str, Any]] | None = None
        with self.app.state.engine.connect() as connection:
            created_rows = connection.execute(
                text(
                    "SELECT id, payload FROM events "
                    "WHERE claim_id = :claim_ref AND type = 'review.created' "
                    "ORDER BY seq"
                ),
                {"claim_ref": claim_ref},
            ).all()
            for event_ref, raw in created_rows:
                payload = _payload(raw)
                current_subtype = payload.get("subtype")
                if (
                    payload.get("type") == "EXCEPTION"
                    and current_subtype in _CHASE_EXCEPTION_SUBTYPES
                    and (subtype is None or current_subtype == subtype)
                    and payload.get("checklist_id") == checklist_ref
                    and isinstance(payload.get("review_id"), str)
                ):
                    created = (str(event_ref), payload)
            if created is None:
                return None

            event_ref, payload = created
            review_ids = {str(payload["review_id"])}
            projected_id = connection.execute(
                text(
                    "SELECT id FROM review_items "
                    "WHERE source_event_id = :source_event_id"
                ),
                {"source_event_id": event_ref},
            ).scalar()
            if isinstance(projected_id, str):
                review_ids.add(projected_id)
            resolved_rows = connection.execute(
                text(
                    "SELECT id, payload FROM events "
                    "WHERE claim_id = :claim_ref AND type = 'review.resolved' "
                    "ORDER BY seq"
                ),
                {"claim_ref": claim_ref},
            ).all()

        resolution: str | None = None
        resolution_event_ref: str | None = None
        for resolved_ref, raw in resolved_rows:
            resolved = _payload(raw)
            if resolved.get("review_id") not in review_ids:
                continue
            value = resolved.get("resolution")
            if value not in _CONTINUE_RESOLUTIONS | {"rejected"}:
                raise ValueError("chase review resolution is outside the closed vocabulary")
            resolution = str(value)
            resolution_event_ref = str(resolved_ref)
        write_id = payload.get("write_id")
        return _ReviewState(
            event_ref=event_ref,
            subtype=str(payload["subtype"]),
            resolution=resolution,
            resolution_event_ref=resolution_event_ref,
            write_id=str(write_id) if isinstance(write_id, str) else None,
        )

    @staticmethod
    def _hash_state(
        *,
        checklist_status: str,
        claim_status: str | None,
        items: list[ChaseItem],
    ) -> str:
        control = {
            "checklist_status": checklist_status,
            "claim_suppressed": claim_status in SUPPRESSED_STATES,
            "items": [
                {
                    "state": item.state,
                    "reminder_count": item.reminder_count,
                    "has_wake": item.next_reminder_at is not None,
                    "snoozed": item.snooze_until is not None,
                }
                for item in items
            ],
        }
        return hashlib.sha256(
            json.dumps(control, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _load(self, command: ControlCommand) -> ControlResult:
        _run_ref, checklist_ref = self._refs(command)
        now = aware(self.app.state.clock())
        with self.sessions() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            claim_ref = checklist.claim_id
            checklist_status = checklist.status
            purpose = checklist.purpose
            initial_required = purpose == "claim_docs" and session.scalar(
                select(ChaseItem.id)
                .where(
                    ChaseItem.checklist_id == checklist_ref,
                    ChaseItem.state == "pending",
                    ChaseItem.requested_at.is_(None),
                )
                .limit(1)
            ) is not None
        claim_status = self._claim_status(claim_ref)

        if (
            checklist_status == "open"
            and claim_status not in SUPPRESSED_STATES
            and not initial_required
        ):
            self._persist_recent_deferral(checklist_ref, claim_ref, now)

        with self.sessions() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            items = list(
                session.scalars(
                    select(ChaseItem)
                    .where(ChaseItem.checklist_id == checklist_ref)
                    .order_by(ChaseItem.item_id, ChaseItem.id)
                )
            )
            checklist_status = checklist.status

        payload_hash = self._hash_state(
            checklist_status=checklist_status,
            claim_status=claim_status,
            items=items,
        )
        event_ref = self._latest_checklist_event(claim_ref, checklist_ref)
        common = {
            "event_ref": event_ref,
            "payload_hash": payload_hash,
        }

        if claim_status in SUPPRESSED_STATES or checklist_status == "cancelled":
            return ControlResult(status="cancelled", step_id=_STEP_TERMINAL, **common)
        if checklist_status == "complete":
            return ControlResult(status="completed", step_id=_STEP_TERMINAL, **common)

        outstanding = [item for item in items if item.state in OUTSTANDING_STATES]
        if not outstanding:
            return ControlResult(status="completed", step_id=_STEP_TERMINAL, **common)

        review = self._review_state(claim_ref, checklist_ref)
        if review is not None and review.resolution == "rejected":
            if review.subtype == "chase_exhausted":
                return ControlResult(
                    status="cancelled",
                    step_id=_STEP_TERMINAL,
                    **common,
                )
            # Register #290: rejecting a recoverable dependency/write
            # exception refuses an automatic retry; it does not destroy the
            # underlying document collection. Remain durably Signal-driven.
            return ControlResult(status="running", step_id=_STEP_WAIT, **common)
        if review is not None and review.resolution is None:
            return ControlResult(
                status="awaiting_review",
                step_id=(
                    _STEP_EXHAUSTED
                    if review.subtype == "chase_exhausted"
                    else _STEP_WAIT
                ),
                review_event_ref=review.event_ref,
                **common,
            )

        initial_items = [
            item
            for item in outstanding
            if item.state == "pending" and item.requested_at is None
        ]
        if purpose == "claim_docs" and initial_items:
            deferred_initial = [
                aware(item.next_reminder_at)
                for item in initial_items
                if item.next_reminder_at is not None
            ]
            if (
                len(deferred_initial) == len(initial_items)
                and min(deferred_initial) > now
            ):
                wake = min(deferred_initial)
                return ControlResult(
                    status="running",
                    step_id=_STEP_WAIT,
                    wake_at_epoch_ms=int(wake.timestamp() * 1000),
                    **common,
                )
            return ControlResult(status="running", step_id=_STEP_INITIAL, **common)

        cap = int(self.config["reminder_cap"])
        capped_due = [
            item
            for item in outstanding
            if item.next_reminder_at is not None
            and aware(item.next_reminder_at) <= now
            and item.reminder_count >= cap
            and (item.snooze_until is None or aware(item.snooze_until) <= now)
        ]
        exhausted_review = self._review_state(
            claim_ref,
            checklist_ref,
            subtype="chase_exhausted",
        )
        if capped_due and exhausted_review is None:
            return ControlResult(status="running", step_id=_STEP_EXHAUSTED, **common)
        # EXCEPTION@1 has no post-cap cadence field. Register #287 therefore
        # maps approved/edited to an explicit continue-without-seventh-reminder:
        # remain durably Signal-driven until the checklist changes or closes.
        if (
            capped_due
            and exhausted_review is not None
            and exhausted_review.resolution in _CONTINUE_RESOLUTIONS
        ):
            return ControlResult(status="running", step_id=_STEP_WAIT, **common)

        wakes: list[datetime] = []
        for item in outstanding:
            if item.next_reminder_at is None:
                continue
            wake = aware(item.next_reminder_at)
            if item.snooze_until is not None:
                wake = max(wake, aware(item.snooze_until))
            wakes.append(wake)
        if not wakes:
            return ControlResult(status="running", step_id=_STEP_WAIT, **common)
        wake = min(wakes)
        due_items = [
            item
            for item in outstanding
            if item.reminder_count < cap
            and item.next_reminder_at is not None
            and max(
                aware(item.next_reminder_at),
                aware(item.snooze_until)
                if item.snooze_until is not None
                else aware(item.next_reminder_at),
            )
            <= now
        ]
        return ControlResult(
            status="running",
            step_id=_STEP_REMINDER if wake <= now else _STEP_WAIT,
            wake_at_epoch_ms=max(0, int(wake.timestamp() * 1000)),
            attempt_no=(
                max(item.reminder_count for item in due_items) + 1
                if due_items
                else None
            ),
            **common,
        )

    # -- six master-plan Activity boundaries ----------------------------

    @activity.defn(name="record_chase_started")
    async def record_chase_started(self, command: ControlCommand) -> ControlResult:
        try:
            return await asyncio.to_thread(self._record_started, command)
        except Exception:
            LOGGER.exception("chase start projection failed")
            raise sanitised_application_error("activity_internal") from None

    def _record_started(self, command: ControlCommand) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        info = activity.info()
        kind, subject_ref = parse_workflow_ref(info.workflow_id)
        if kind != "chase" or subject_ref != checklist_ref:
            raise ValueError("workflow identity does not match chase checklist")
        self.projection.record_started(
            run_ref=run_ref,
            workflow_ref=info.workflow_id,
            workflow_run_ref=info.workflow_run_id,
            workflow_type=info.workflow_type,
            worker_build_id=self.worker_build_id,
        )
        self._step(
            run_ref,
            _STEP_START,
            status="completed",
            event_ref=command.trigger_event_ref,
        )
        return ControlResult(
            status="running",
            run_ref=run_ref,
            event_ref=command.trigger_event_ref,
            step_id=_STEP_START,
        )

    @activity.defn(name="load_chase_state")
    async def load_chase_state(self, command: ControlCommand) -> ControlResult:
        try:
            result = await asyncio.to_thread(self._load, command)
            await asyncio.to_thread(
                self._step,
                command.run_ref,
                _STEP_LOAD,
                status="completed",
                event_ref=result.event_ref,
            )
            return result
        except Exception:
            LOGGER.exception("chase state load failed")
            raise sanitised_application_error("activity_internal") from None

    @activity.defn(name="apply_chase_event")
    async def apply_chase_event(self, command: ControlCommand) -> ControlResult:
        try:
            return await asyncio.to_thread(self._apply_event, command)
        except Exception:
            LOGGER.exception("chase wake application failed")
            raise sanitised_application_error("domain_rejected") from None

    def _apply_event(self, command: ControlCommand) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        with self.sessions() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            claim_ref = checklist.claim_id

        if command.event_ref is not None:
            with self.app.state.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT type, payload, claim_id, actor, seq FROM events "
                        "WHERE id = :event_ref"
                    ),
                    {"event_ref": command.event_ref},
                ).first()
            if (
                row is None
                or str(row[0]) not in _WAKE_EVENT_TYPES
                or str(row[2]) != claim_ref
                or _payload(row[1]).get("checklist_id") != checklist_ref
            ):
                raise ValueError("wake event is not attached to this checklist")
            event_type = str(row[0])
            actor = str(row[3])
            event_seq = int(row[4])
            if not self.checklist.authorise_wake_actor(event_type, actor):
                raise ValueError("wake event actor is not authorised")
            high_water = self._applied_event_seq(run_ref)
            if event_seq <= high_water:
                return ControlResult(
                    status="running",
                    run_ref=run_ref,
                    event_ref=command.event_ref,
                    event_seq=high_water,
                    step_id=_STEP_APPLY,
                )
            if event_type == "chase.inbound_received":
                self._persist_recent_deferral(
                    checklist_ref,
                    claim_ref,
                    aware(self.app.state.clock()),
                )
            self.projection.record_status(
                ControlResult(
                    status="running",
                    run_ref=run_ref,
                    event_ref=command.event_ref,
                    step_id=_STEP_APPLY,
                    event_seq=event_seq,
                )
            )
        else:
            self._persist_recent_deferral(
                checklist_ref,
                claim_ref,
                aware(self.app.state.clock()),
            )
        self._step(
            run_ref,
            _STEP_APPLY,
            status="completed",
            event_ref=command.event_ref,
            event_seq=event_seq if command.event_ref is not None else None,
        )
        return ControlResult(
            status="running",
            run_ref=run_ref,
            event_ref=command.event_ref,
            event_seq=event_seq if command.event_ref is not None else None,
            step_id=_STEP_APPLY,
        )

    @activity.defn(name="governed_chase_send")
    async def governed_chase_send(self, command: ControlCommand) -> ControlResult:
        try:
            return await asyncio.to_thread(self._governed_send, command)
        except Exception:
            LOGGER.exception("governed chase send failed")
            # The Activity has one attempt and may have crossed the AR-2 gate
            # before the Worker lost the outcome. The Workflow converts this
            # closed failure type into EXCEPTION{uncertain_write}; it never
            # blindly repeats the write.
            raise sanitised_application_error(
                "uncertain_write",
                details={
                    "run_ref": command.run_ref,
                    "write_id": command.write_id,
                },
            ) from None

    def _awaiting_exception(
        self,
        command: ControlCommand,
        *,
        claim_ref: str,
        subtype: str,
        facts: dict[str, Any],
        risk: str,
        recommendation: str,
    ) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        review = self._review_state(
            claim_ref,
            checklist_ref,
            subtype=subtype,
        )
        identity: dict[str, Any] = {
            "checklist_id": checklist_ref,
            "write_id": command.write_id,
        }
        if review is not None and review.resolution_event_ref is not None:
            identity["authorised_by"] = review.resolution_event_ref
        if (
            review is not None
            and review.subtype == subtype
            and review.resolution is None
        ):
            event_ref = review.event_ref
        else:
            event_ref = self.checklist.exception_once(
                claim_id=claim_ref,
                subtype=subtype,
                identity=identity,
                payload={
                    "facts": facts,
                    "risk": risk,
                    "recommendation": recommendation,
                    "resolution_schema": "EXCEPTION@1",
                    "role": self.config["exception_routing_role"],
                },
            )
        result = ControlResult(
            status="awaiting_review",
            run_ref=run_ref,
            event_ref=event_ref,
            review_event_ref=event_ref,
            write_id=command.write_id,
            step_id=command.step_id or _STEP_WAIT,
        )
        self.projection.record_status(result)
        self._step(
            run_ref,
            command.step_id or _STEP_WAIT,
            status="awaiting_review",
            event_ref=event_ref,
            write_id=command.write_id,
        )
        return result

    def _governed_send(self, command: ControlCommand) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        if command.write_id is None:
            raise ValueError("governed chase send requires write_id")
        now = aware(self.app.state.clock())

        with self.sessions() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            session.expunge(checklist)
        if (
            checklist.status != "open"
            or self._claim_status(checklist.claim_id) in SUPPRESSED_STATES
        ):
            return ControlResult(
                status="cancelled",
                run_ref=run_ref,
                write_id=command.write_id,
                step_id=_STEP_TERMINAL,
            )

        with self.sessions() as session:
            outstanding = list(
                session.scalars(
                    select(ChaseItem)
                    .where(
                        ChaseItem.checklist_id == checklist_ref,
                        ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                    )
                    .order_by(ChaseItem.item_id, ChaseItem.id)
                )
            )

        initial = checklist.purpose == "claim_docs" and any(
            item.state == "pending" and item.requested_at is None
            for item in outstanding
        )
        if not initial:
            self._persist_recent_deferral(checklist_ref, checklist.claim_id, now)
            with self.sessions() as session:
                outstanding = list(
                    session.scalars(
                        select(ChaseItem)
                        .where(
                            ChaseItem.checklist_id == checklist_ref,
                            ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                        )
                        .order_by(ChaseItem.item_id, ChaseItem.id)
                    )
                )
        cap = int(self.config["reminder_cap"])
        due = [
            item
            for item in outstanding
            if item.next_reminder_at is not None
            and aware(item.next_reminder_at) <= now
            and item.reminder_count < cap
            and (item.snooze_until is None or aware(item.snooze_until) <= now)
        ]
        if not initial and not due:
            return ControlResult(
                status="running",
                run_ref=run_ref,
                write_id=command.write_id,
                step_id=_STEP_LOAD,
            )
        reminder_index = (
            0
            if initial
            else max(item.reminder_count for item in due) + 1
        )
        expected_write_id = f"chase:{checklist_ref.lower()}:{reminder_index}"
        if command.write_id != expected_write_id:
            # A normal document/snooze/inbound race invalidated the timer.
            # Reload authoritative state; never fail the finite Workflow.
            return ControlResult(
                status="running",
                run_ref=run_ref,
                write_id=command.write_id,
                step_id=_STEP_LOAD,
            )

        step_id = _STEP_INITIAL if initial else _STEP_REMINDER
        self._step(
            run_ref,
            step_id,
            status="running",
            write_id=command.write_id,
        )

        if initial:
            prior_review = self._review_state(checklist.claim_id, checklist_ref)
            outcome_status = self.checklist.ensure_initial_request(
                checklist_ref,
                checklist.claim_id,
                now=now,
                run_id=run_ref,
                authorisation_event_ref=(
                    prior_review.resolution_event_ref
                    if prior_review is not None
                    else None
                ),
            )
            if outcome_status == "queued_window":
                wake = self.app.state.agent_runtime.comms.next_send_window(now)
                with self.sessions.begin() as session:
                    items = session.scalars(
                        select(ChaseItem).where(
                            ChaseItem.checklist_id == checklist_ref,
                            ChaseItem.state == "pending",
                            ChaseItem.requested_at.is_(None),
                        )
                    )
                    for item in items:
                        item.next_reminder_at = wake
                self._step(
                    run_ref,
                    _STEP_INITIAL,
                    status="completed",
                    write_id=command.write_id,
                )
                return ControlResult(
                    status="running",
                    run_ref=run_ref,
                    write_id=command.write_id,
                    step_id=_STEP_WAIT,
                    wake_at_epoch_ms=int(wake.timestamp() * 1000),
                )
            accepted = outcome_status in {"staged", "executed", "existing"}
            if not accepted:
                current_review = self._review_state(
                    checklist.claim_id,
                    checklist_ref,
                )
                if current_review is not None and current_review.resolution is None:
                    return self._awaiting_exception(
                        command,
                        claim_ref=checklist.claim_id,
                        subtype=current_review.subtype,
                        facts={"write_id": command.write_id},
                        risk="the initial document request is not releasable",
                        recommendation="resolve the visible blocker before another attempt",
                    )
                return self._awaiting_exception(
                    command,
                    claim_ref=checklist.claim_id,
                    subtype="chase_send_refused",
                    facts={
                        "write_id": command.write_id,
                        "outcome": outcome_status,
                    },
                    risk="the initial document request was not staged or executed",
                    recommendation="verify the template and gate before authorising retry",
                )
            self._step(
                run_ref,
                _STEP_INITIAL,
                status="completed",
                write_id=command.write_id,
            )
            return ControlResult(
                status="running",
                run_ref=run_ref,
                write_id=command.write_id,
                step_id=_STEP_INITIAL,
            )

        requester, tone = self.checklist.requester(
            checklist.claim_id,
            checklist.requester_party_id,
        )
        if requester is None:
            return self._awaiting_exception(
                command,
                claim_ref=checklist.claim_id,
                subtype="chase_requester_missing",
                facts={"items": [item.item_id for item in due]},
                risk="the reminder has no uniquely captured requester",
                recommendation="capture the requester before authorising another attempt",
            )

        recipients = [requester]
        if (
            checklist.purpose != "assessor_report"
            and reminder_index >= int(self.config["cc_insured_from_reminder"])
        ):
            insured = self.checklist.insured_party(checklist.claim_id)
            if insured is not None and insured not in recipients:
                recipients.append(insured)
        context = self.checklist.summary_payload(
            checklist_ref,
            now=now,
            include_snoozed=False,
        )
        outcome = self.app.state.agent_runtime.comms.send(
            template_id=f"T-06r-{tone}",
            claim_id=checklist.claim_id,
            to_party_ids=recipients,
            attachments=(),
            capability_id="chase.reminder",
            actor=ACTOR,
            run_id=run_ref,
            action_payload=context,
        )
        if outcome["status"] == "queued_window":
            wake = self.app.state.agent_runtime.comms.next_send_window(now)
            due_ids = {item.id for item in due}
            with self.sessions.begin() as session:
                items = session.scalars(
                    select(ChaseItem).where(ChaseItem.id.in_(due_ids))
                )
                for item in items:
                    item.next_reminder_at = wake
            self._step(
                run_ref,
                _STEP_REMINDER,
                status="completed",
                write_id=command.write_id,
            )
            return ControlResult(
                status="running",
                run_ref=run_ref,
                write_id=command.write_id,
                step_id=_STEP_WAIT,
                wake_at_epoch_ms=int(wake.timestamp() * 1000),
            )
        if outcome["status"] not in {"staged", "executed"}:
            return self._awaiting_exception(
                command,
                claim_ref=checklist.claim_id,
                subtype="chase_send_refused",
                facts={
                    "write_id": command.write_id,
                    "outcome": str(outcome["status"]),
                    "code": outcome.get("code"),
                },
                risk="the reminder was not staged or executed",
                recommendation="verify the template, gate and transport before retry",
            )

        due_ids = {item.id for item in due}
        with self.sessions.begin() as session:
            current_checklist = session.get(ChaseChecklist, checklist_ref)
            if current_checklist is None or current_checklist.status != "open":
                raise RuntimeError("checklist changed after governed send")
            current_items = list(
                session.scalars(
                    select(ChaseItem)
                    .where(ChaseItem.id.in_(due_ids))
                    .order_by(ChaseItem.item_id, ChaseItem.id)
                )
            )
            sent_items: list[str] = []
            for item in current_items:
                if (
                    item.state not in OUTSTANDING_STATES
                    or item.next_reminder_at is None
                    or aware(item.next_reminder_at) > now
                    or item.reminder_count >= cap
                    or (
                        item.snooze_until is not None
                        and aware(item.snooze_until) > now
                    )
                ):
                    continue
                item.reminder_count += 1
                item.next_reminder_at = self._next_reminder(item, item.reminder_count)
                sent_items.append(item.item_id)
            if outcome["status"] == "executed" and sent_items:
                self.checklist.emit_event(
                    session,
                    claim_id=checklist.claim_id,
                    event_type="chase.reminder_sent",
                    payload={
                        "claim_id": checklist.claim_id,
                        "checklist_id": checklist_ref,
                        "items": sent_items,
                        "write_id": command.write_id,
                    },
                    correlation_id=run_ref,
                )
        self._step(
            run_ref,
            _STEP_REMINDER,
            status="completed",
            write_id=command.write_id,
        )
        return ControlResult(
            status="running",
            run_ref=run_ref,
            write_id=command.write_id,
            step_id=_STEP_REMINDER,
        )

    def _next_reminder(self, item: ChaseItem, new_count: int) -> datetime:
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

    @activity.defn(name="create_chase_exception")
    async def create_chase_exception(self, command: ControlCommand) -> ControlResult:
        try:
            return await asyncio.to_thread(self._create_exception, command)
        except Exception:
            LOGGER.exception("chase exception creation failed")
            raise sanitised_application_error("activity_internal") from None

    def _create_exception(self, command: ControlCommand) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        with self.sessions() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            claim_ref = checklist.claim_id

        if command.write_id is not None:
            return self._awaiting_exception(
                command,
                claim_ref=claim_ref,
                subtype="uncertain_write",
                facts={"write_id": command.write_id},
                risk="the governed write may have executed before its outcome was lost",
                recommendation=(
                    "inspect the outbound target and approve retry only after "
                    "independent non-execution confirmation"
                ),
            )

        review = self._review_state(
            claim_ref,
            checklist_ref,
            subtype="chase_exhausted",
        )
        if review is not None and review.resolution in _CONTINUE_RESOLUTIONS:
            return ControlResult(
                status="running",
                run_ref=run_ref,
                event_ref=review.resolution_event_ref,
                review_event_ref=review.event_ref,
                step_id=_STEP_WAIT,
            )
        if review is not None and review.resolution == "rejected":
            return ControlResult(
                status="cancelled",
                run_ref=run_ref,
                event_ref=review.resolution_event_ref,
                review_event_ref=review.event_ref,
                step_id=_STEP_TERMINAL,
            )
        existing_event_ref = review.event_ref if review is not None else None
        if existing_event_ref is None:
            with self.sessions() as session:
                checklist = session.get(ChaseChecklist, checklist_ref)
                if checklist is None:
                    raise LookupError("chase checklist was not found")
                items = list(
                    session.scalars(
                        select(ChaseItem)
                        .where(
                            ChaseItem.checklist_id == checklist_ref,
                            ChaseItem.state.in_(tuple(OUTSTANDING_STATES)),
                            ChaseItem.reminder_count >= int(self.config["reminder_cap"]),
                        )
                        .order_by(ChaseItem.item_id, ChaseItem.id)
                    )
                )
                if not items:
                    return ControlResult(
                        status="running",
                        run_ref=run_ref,
                        step_id=_STEP_LOAD,
                    )
                item_ids = [item.item_id for item in items]
            existing_event_ref = self.checklist.exception_once(
                claim_id=claim_ref,
                subtype="chase_exhausted",
                identity={"checklist_id": checklist_ref},
                payload={
                    "items": item_ids,
                    "facts": {"items": item_ids},
                    "risk": "required documents remain outstanding at the reminder cap",
                    "recommendation": (
                        "confirm whether collection should continue or the chase should close"
                    ),
                    "resolution_schema": "EXCEPTION@1",
                    "role": self.config["exception_routing_role"],
                },
            )
        result = ControlResult(
            status="awaiting_review",
            run_ref=run_ref,
            review_event_ref=existing_event_ref,
            step_id=_STEP_EXHAUSTED,
        )
        self.projection.record_status(result)
        self._step(
            run_ref,
            _STEP_EXHAUSTED,
            status="awaiting_review",
            event_ref=existing_event_ref,
        )
        return result

    @activity.defn(name="record_chase_terminal")
    async def record_chase_terminal(self, command: ControlCommand) -> ControlResult:
        try:
            return await asyncio.to_thread(self._record_terminal, command)
        except Exception:
            LOGGER.exception("chase terminal projection failed")
            raise sanitised_application_error("activity_internal") from None

    def _record_terminal(self, command: ControlCommand) -> ControlResult:
        run_ref, checklist_ref = self._refs(command)
        terminal_event_ref = command.event_ref
        with self.sessions.begin() as session:
            checklist = session.get(ChaseChecklist, checklist_ref)
            if checklist is None:
                raise LookupError("chase checklist was not found")
            claim_status = session.execute(
                text("SELECT status FROM claims WHERE id = :claim_ref"),
                {"claim_ref": checklist.claim_id},
            ).scalar()
            review = self._review_state(
                checklist.claim_id,
                checklist_ref,
                subtype="chase_exhausted",
            )
            if checklist.status == "open" and (
                claim_status in SUPPRESSED_STATES
                or (
                    review is not None
                    and review.subtype == "chase_exhausted"
                    and review.resolution == "rejected"
                )
            ):
                checklist.status = "cancelled"
                terminal_event_ref = self.checklist.emit_event(
                    session,
                    claim_id=checklist.claim_id,
                    event_type="chase.cancelled",
                    payload={
                        "claim_id": checklist.claim_id,
                        "checklist_id": checklist.id,
                        "purpose": checklist.purpose,
                    },
                    correlation_id=run_ref,
                )
            elif checklist.status == "open":
                self.checklist.maybe_complete(session, checklist)

            if checklist.status == "complete":
                status = "completed"
            elif checklist.status == "cancelled":
                status = "cancelled"
            else:
                status = "blocked"
        result = ControlResult(
            status=status,
            run_ref=run_ref,
            event_ref=terminal_event_ref,
            step_id=_STEP_TERMINAL,
        )
        self.projection.record_status(result)
        self._step(
            run_ref,
            _STEP_TERMINAL,
            status=status,
            event_ref=terminal_event_ref,
        )
        return result


def chase_activity_registrations(
    activities: ChaseActivities,
) -> tuple[Callable[..., Any], ...]:
    """The exact T03 registration set for the control Worker."""

    return (
        activities.record_chase_started,
        activities.load_chase_state,
        activities.apply_chase_event,
        activities.governed_chase_send,
        activities.create_chase_exception,
        activities.record_chase_terminal,
    )
