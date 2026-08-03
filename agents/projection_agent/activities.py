"""Temporal Activities for the PRD-09 projection lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import sessionmaker
from temporalio import activity

from orchestration.contracts import ControlCommand, ControlResult
from orchestration.errors import sanitised_application_error
from projection_agent.models import Projection

__all__ = [
    "ProjectionActivities",
    "projection_control_activity_registrations",
    "projection_effect_activity_registrations",
]


class ProjectionActivities:
    """Reload projections inside Activities; copied values stay in PostgreSQL."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _row(self, projection_ref: str) -> Projection:
        with self._sessions() as session:
            row = session.get(Projection, projection_ref)
            if row is None:
                raise LookupError("projection was not found")
            session.expunge(row)
            return row

    @staticmethod
    def _result(command: ControlCommand, status: str) -> ControlResult:
        return ControlResult(
            status=status,
            run_ref=command.run_ref,
            claim_ref=command.claim_ref,
            event_ref=command.event_ref,
            projection_ref=command.projection_ref,
            attempt_no=command.attempt_no,
        )

    async def _load(self, command: ControlCommand, checkpoint: int) -> Projection:
        if (
            command.claim_ref is None
            or command.projection_ref != command.run_ref
            or command.attempt_no != checkpoint
        ):
            raise sanitised_application_error("domain_rejected")
        row = await asyncio.to_thread(self._row, command.projection_ref)
        if row.claim_id != command.claim_ref:
            raise sanitised_application_error("domain_rejected")
        return row

    @activity.defn(name="projection_prepare")
    async def prepare(self, command: ControlCommand) -> ControlResult:
        row = await self._load(command, 1)
        if row.status in {"failed", "diverged"}:
            return self._result(command, "blocked")
        return self._result(command, "running")

    @activity.defn(name="projection_execute_or_stage")
    async def execute_or_stage(self, command: ControlCommand) -> ControlResult:
        row = await self._load(command, 2)
        if row.mode == "paste_assist":
            return self._result(command, "running")
        # T06 has no approved executor provider. Refuse instead of guessing one.
        return self._result(command, "blocked")

    @activity.defn(name="projection_readback")
    async def readback(self, command: ControlCommand) -> ControlResult:
        row = await self._load(command, 3)
        if row.status in {"queued", "executing"}:
            return self._result(command, "awaiting_review")
        if row.status in {"failed", "diverged"}:
            return self._result(command, "blocked")
        return self._result(command, "running")

    @activity.defn(name="projection_reconcile")
    async def reconcile(self, command: ControlCommand) -> ControlResult:
        row = await self._load(command, 4)
        if row.status == "verifying":
            await asyncio.to_thread(
                self._app.state.projection_agent.resume,
                actor="agent:projection",
            )
            row = await asyncio.to_thread(self._row, command.projection_ref)
        if row.status in {"failed", "diverged"}:
            return self._result(command, "blocked")
        if row.status != "completed":
            return self._result(command, "awaiting_review")
        return self._result(command, "running")

    @activity.defn(name="projection_record_terminal")
    async def record_terminal(self, command: ControlCommand) -> ControlResult:
        row = await self._load(command, 5)
        return self._result(
            command,
            "completed" if row.status == "completed" else "blocked",
        )


def projection_control_activity_registrations(
    activities: ProjectionActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, ProjectionActivities):
        raise RuntimeError("projection registration requires ProjectionActivities")
    return (
        activities.prepare,
        activities.readback,
        activities.reconcile,
        activities.record_terminal,
    )


def projection_effect_activity_registrations(
    activities: ProjectionActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, ProjectionActivities):
        raise RuntimeError("projection registration requires ProjectionActivities")
    return (activities.execute_or_stage,)
