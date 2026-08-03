"""Temporal Activities binding the eight PRD-05 steps to their domain services."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import sessionmaker
from temporalio import activity

from agent_runtime.models import AgentRun
from orchestration.contracts import ControlCommand, ControlResult
from orchestration.errors import sanitised_application_error

__all__ = [
    "IntakeActivities",
    "intake_activity_registrations",
    "intake_control_activity_registrations",
    "intake_effect_activity_registrations",
]

_STEPS = (
    "create_claim",
    "ingest",
    "populate",
    "dupe_check",
    "late_check",
    "acknowledge",
    "checklist",
    "triage",
)


class IntakeActivities:
    """Execute one persisted COP boundary per Activity invocation."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _snapshot(self, run_ref: str) -> tuple[str, str | None, list[dict[str, Any]]]:
        with self._sessions() as session:
            run = session.get(AgentRun, run_ref)
            if run is None or run.capability_id != "intake.claim_creation":
                raise LookupError("intake run projection was not found")
            return run.status, run.claim_id, [dict(step) for step in run.steps]

    async def _execute(
        self,
        command: ControlCommand,
        *,
        step_id: str,
        checkpoint: int,
    ) -> ControlResult:
        if (
            command.trigger_event_ref is None
            or command.attempt_no != checkpoint
            or step_id != _STEPS[checkpoint - 1]
        ):
            raise sanitised_application_error("domain_rejected")
        if (
            command.event_ref is not None
            and command.event_ref != command.trigger_event_ref
        ):
            await asyncio.to_thread(
                self._app.state.agent_runtime.apply_control_event,
                command.run_ref,
                command.event_ref,
            )
        status, claim_ref, steps = await asyncio.to_thread(
            self._snapshot, command.run_ref
        )
        if status == "completed":
            return ControlResult(
                status="completed",
                run_ref=command.run_ref,
                claim_ref=claim_ref,
                step_id=step_id,
                attempt_no=checkpoint,
            )
        if status in {"blocked", "failed", "cancelled"}:
            return ControlResult(
                status="blocked",
                run_ref=command.run_ref,
                claim_ref=claim_ref,
                step_id=step_id,
                attempt_no=checkpoint,
            )
        current = next(
            (step for step in steps if step.get("step_id") == step_id), None
        )
        if current is None:
            raise sanitised_application_error("domain_rejected")
        if current.get("status") != "completed":
            await asyncio.to_thread(
                self._app.state.agent_runtime.run_cop_activity,
                command.run_ref,
                step_id,
            )
        status, claim_ref, steps = await asyncio.to_thread(
            self._snapshot, command.run_ref
        )
        current = next(step for step in steps if step.get("step_id") == step_id)
        if status == "completed":
            control_status = "completed"
        elif current.get("status") in {"waiting", "awaiting_review"} or status == (
            "awaiting_review"
        ):
            control_status = "awaiting_review"
        elif status in {"blocked", "failed", "cancelled"}:
            control_status = "blocked"
        else:
            control_status = "running"
        return ControlResult(
            status=control_status,
            run_ref=command.run_ref,
            claim_ref=claim_ref,
            step_id=step_id,
            attempt_no=checkpoint,
        )

    @activity.defn(name="intake_create_claim")
    async def create_claim(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="create_claim", checkpoint=1)

    @activity.defn(name="intake_ingest")
    async def ingest(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="ingest", checkpoint=2)

    @activity.defn(name="intake_populate")
    async def populate(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="populate", checkpoint=3)

    @activity.defn(name="intake_dupe_check")
    async def dupe_check(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="dupe_check", checkpoint=4)

    @activity.defn(name="intake_late_check")
    async def late_check(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="late_check", checkpoint=5)

    @activity.defn(name="intake_acknowledge")
    async def acknowledge(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="acknowledge", checkpoint=6)

    @activity.defn(name="intake_checklist")
    async def checklist(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="checklist", checkpoint=7)

    @activity.defn(name="intake_triage")
    async def triage(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, step_id="triage", checkpoint=8)


def intake_activity_registrations(
    activities: IntakeActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, IntakeActivities):
        raise RuntimeError("intake activity registration requires IntakeActivities")
    return (
        activities.create_claim,
        activities.ingest,
        activities.populate,
        activities.dupe_check,
        activities.late_check,
        activities.acknowledge,
        activities.checklist,
        activities.triage,
    )


def intake_control_activity_registrations(
    activities: IntakeActivities,
) -> tuple[Callable[..., Any], ...]:
    registered = intake_activity_registrations(activities)
    return (*registered[:5], *registered[6:])


def intake_effect_activity_registrations(
    activities: IntakeActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, IntakeActivities):
        raise RuntimeError("intake activity registration requires IntakeActivities")
    return (activities.acknowledge,)
