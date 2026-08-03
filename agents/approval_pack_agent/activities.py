"""Temporal Activities projecting PRD-08 approval-pack state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import sessionmaker
from temporalio import activity

from agent_runtime.models import AgentRun
from orchestration.contracts import ControlCommand, ControlResult
from orchestration.errors import sanitised_application_error

__all__ = ["ApprovalPackActivities", "approval_activity_registrations"]

_ACTIVITY_NAMES = (
    "approval_resolve_manifest",
    "approval_merge",
    "approval_generate_note",
    "approval_grade_and_queue",
    "approval_apply_review",
    "approval_prepare_signature",
    "approval_finalize_signature",
    "approval_record_terminal",
)


class ApprovalPackActivities:
    """Reload the durable run projection; artifacts never cross the boundary."""

    def __init__(self, app: Any) -> None:
        self._sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _run(self, run_ref: str) -> AgentRun:
        with self._sessions() as session:
            run = session.get(AgentRun, run_ref)
            if run is None or run.capability_id not in {"pack.merge", "pack.note_draft"}:
                raise LookupError("approval-pack run projection was not found")
            session.expunge(run)
            return run

    async def _observe(
        self, command: ControlCommand, *, checkpoint: int, terminal: bool = False
    ) -> ControlResult:
        if command.claim_ref is None or command.attempt_no != checkpoint:
            raise sanitised_application_error("domain_rejected")
        run = await asyncio.to_thread(self._run, command.run_ref)
        if run.claim_id != command.claim_ref:
            raise sanitised_application_error("domain_rejected")
        status = (
            "blocked"
            if run.status in {"blocked", "failed", "cancelled"}
            else "completed"
            if terminal and run.status == "completed"
            else "awaiting_review"
            if run.status == "awaiting_review"
            else "running"
        )
        return ControlResult(
            status=status,
            run_ref=command.run_ref,
            claim_ref=command.claim_ref,
            event_ref=command.event_ref,
            attempt_no=checkpoint,
        )

    @activity.defn(name="approval_resolve_manifest")
    async def resolve_manifest(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=1)

    @activity.defn(name="approval_merge")
    async def merge(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=2)

    @activity.defn(name="approval_generate_note")
    async def generate_note(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=3)

    @activity.defn(name="approval_grade_and_queue")
    async def grade_and_queue(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=4)

    @activity.defn(name="approval_apply_review")
    async def apply_review(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=5)

    @activity.defn(name="approval_prepare_signature")
    async def prepare_signature(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=6)

    @activity.defn(name="approval_finalize_signature")
    async def finalize_signature(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=7)

    @activity.defn(name="approval_record_terminal")
    async def record_terminal(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=8, terminal=True)


def approval_activity_registrations(
    activities: ApprovalPackActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, ApprovalPackActivities):
        raise RuntimeError("approval registration requires ApprovalPackActivities")
    return (
        activities.resolve_manifest,
        activities.merge,
        activities.generate_note,
        activities.grade_and_queue,
        activities.apply_review,
        activities.prepare_signature,
        activities.finalize_signature,
        activities.record_terminal,
    )
