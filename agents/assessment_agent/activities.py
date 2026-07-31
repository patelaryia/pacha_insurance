"""Temporal Activities binding PRD-07 orchestration to existing domain services."""

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
    "AssessmentActivities",
    "assessment_control_activity_registrations",
    "assessment_effect_activity_registrations",
]

_ACTIVITY_NAMES = (
    "assessment_prepare",
    "assessment_mode_shadow",
    "assessment_apply_mode_review",
    "assessment_dispatch",
    "assessment_parse_report",
    "assessment_cascade",
    "assessment_record_terminal",
)


class AssessmentActivities:
    """Reload assessment state inside the Worker; history carries references only."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _run(self, run_ref: str) -> AgentRun:
        with self._sessions() as session:
            run = session.get(AgentRun, run_ref)
            if run is None or run.capability_id != "assessment.mode_shadow":
                raise LookupError("assessment run projection was not found")
            session.expunge(run)
            return run

    async def _observe(
        self,
        command: ControlCommand,
        *,
        checkpoint: int,
        execute_shadow: bool = False,
    ) -> ControlResult:
        if command.claim_ref is None or command.attempt_no != checkpoint:
            raise sanitised_application_error("domain_rejected")
        run = await asyncio.to_thread(self._run, command.run_ref)
        if run.claim_id != command.claim_ref:
            raise sanitised_application_error("domain_rejected")
        if execute_shadow and run.status in {"pending", "running"}:
            await asyncio.to_thread(
                self._app.state.agent_runtime.run_cop_activity,
                command.run_ref,
                "call_model",
            )
            run = await asyncio.to_thread(self._run, command.run_ref)
        status = (
            "blocked"
            if run.status in {"blocked", "failed", "cancelled"}
            else "awaiting_review"
            if run.status == "awaiting_review"
            else "running"
        )
        return ControlResult(
            status=status,
            run_ref=command.run_ref,
            claim_ref=command.claim_ref,
            attempt_no=checkpoint,
        )

    @activity.defn(name="assessment_prepare")
    async def prepare(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=1)

    @activity.defn(name="assessment_mode_shadow")
    async def mode_shadow(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=2, execute_shadow=True)

    @activity.defn(name="assessment_apply_mode_review")
    async def apply_mode_review(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=3)

    @activity.defn(name="assessment_dispatch")
    async def dispatch(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=4)

    @activity.defn(name="assessment_parse_report")
    async def parse_report(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=5)

    @activity.defn(name="assessment_cascade")
    async def cascade(self, command: ControlCommand) -> ControlResult:
        return await self._observe(command, checkpoint=6)

    @activity.defn(name="assessment_record_terminal")
    async def record_terminal(self, command: ControlCommand) -> ControlResult:
        result = await self._observe(command, checkpoint=7)
        if result.status == "running":
            return ControlResult(
                status="completed",
                run_ref=result.run_ref,
                claim_ref=result.claim_ref,
                attempt_no=7,
            )
        return result


def assessment_control_activity_registrations(
    activities: AssessmentActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, AssessmentActivities):
        raise RuntimeError("assessment registration requires AssessmentActivities")
    return (
        activities.prepare,
        activities.mode_shadow,
        activities.apply_mode_review,
        activities.parse_report,
        activities.cascade,
        activities.record_terminal,
    )


def assessment_effect_activity_registrations(
    activities: AssessmentActivities,
) -> tuple[Callable[..., Any], ...]:
    if not isinstance(activities, AssessmentActivities):
        raise RuntimeError("assessment registration requires AssessmentActivities")
    return (activities.dispatch,)
