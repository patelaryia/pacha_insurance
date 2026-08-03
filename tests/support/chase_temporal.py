"""Real Temporal driver for document-chase acceptance fixtures.

This module owns infrastructure only: it starts the production Worker,
delivers committed intents through the production outbox Workflow, and advances
the time-skipping server with the application's injected clock. Workflow
decisions remain exclusively in ``DocumentChaseWorkflow``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment

from assessment_agent import (
    AssessmentActivities,
    assessment_control_activity_registrations,
    assessment_effect_activity_registrations,
)
from assessment_agent.workflows import AssessmentWorkflow
from chase_agent import chase_activity_registrations
from claim_core.service import new_ulid
from doc_intel.activities import (
    DocumentIntelligenceActivities,
    docintel_activity_registrations,
)
from doc_intel.workflows import DocumentIntelligenceWorkflow
from intake_agent import (
    IntakeActivities,
    intake_control_activity_registrations,
    intake_effect_activity_registrations,
)
from intake_agent.workflows import IntakeWorkflow
from orchestration.activities import SystemActivities
from orchestration.chase_workflow import DocumentChaseWorkflow
from orchestration.contracts import ControlPayloadInterceptor, ControlResult
from orchestration.ids import agent_workflow_ref
from orchestration.starter import (
    TEMPORAL_INTENT_MAPPINGS,
    TemporalIntentConsumer,
    TemporalStarter,
)
from orchestration.worker import build_worker
from orchestration.workflows import OutboxDrainWorkflow
from support.temporal import local_config, static_data_converter

_DRAIN_WAVES = 3
_TASK_SETTLE_SECONDS = 0.5


class ChaseTemporalDriver:
    """Run an acceptance-test application through the real chase Workflow."""

    def __init__(self, domain: Any, *, include_chase: bool = True) -> None:
        self.domain = domain
        self.include_chase = include_chase
        self.config = local_config()
        self.loop = asyncio.new_event_loop()
        self.temporal: WorkflowEnvironment | None = None
        self.worker = None
        self.docintel_worker = None
        self.effects_worker = None

    def run(self, awaitable):  # noqa: ANN001, ANN201 - synchronous test bridge
        return self.loop.run_until_complete(awaitable)

    def start(self) -> ChaseTemporalDriver:
        asyncio.set_event_loop(self.loop)
        self.temporal = self.run(
            WorkflowEnvironment.start_time_skipping(
                data_converter=static_data_converter(self.config),
                interceptors=[ControlPayloadInterceptor()],
            )
        )
        if hasattr(self.domain, "clock"):
            temporal_now = self.run(self.temporal.get_current_time())
            aligned = temporal_now.astimezone(UTC).replace(
                hour=6,
                minute=0,
                second=0,
                microsecond=0,
            )
            aligned += timedelta(days=(-aligned.weekday()) % 7)
            if aligned <= temporal_now:
                aligned += timedelta(days=7)
            if aligned > temporal_now:
                self.run(self.temporal.sleep(aligned - temporal_now))
            self.domain.clock.advance_to(aligned)
            self.domain.started_at = aligned

        starter = TemporalStarter(self.temporal.client, self.config)
        mappings = (
            TEMPORAL_INTENT_MAPPINGS
            if self.include_chase
            else tuple(
                mapping
                for mapping in TEMPORAL_INTENT_MAPPINGS
                if not mapping.event_type.startswith("chase.")
            )
        )
        self.domain.app.state.dispatcher.register_consumer(
            "temporal_intent",
            TemporalIntentConsumer(starter, mappings),
        )
        system = SystemActivities(self.domain.app)
        chase_agent = (
            getattr(self.domain.app.state, "chase_agent", None)
            if self.include_chase
            else None
        )
        chase = (
            chase_agent.temporal_activities(worker_build_id=self.config.build_id)
            if chase_agent is not None
            else None
        )
        docintel = DocumentIntelligenceActivities(
            self.domain.app.state.doc_intel,
            worker_build_id=self.config.build_id,
        )
        intake = IntakeActivities(self.domain.app)
        assessment = AssessmentActivities(self.domain.app)

        async def _workers():
            control_worker = build_worker(
                self.temporal.client,
                self.config,
                role="control",
                workflows=[
                    OutboxDrainWorkflow,
                    DocumentIntelligenceWorkflow,
                    IntakeWorkflow,
                    AssessmentWorkflow,
                    *([DocumentChaseWorkflow] if chase is not None else []),
                ],
                activities=[
                    system.dispatch_nonledger_events,
                    *(chase_activity_registrations(chase) if chase is not None else ()),
                    *intake_control_activity_registrations(intake),
                    *assessment_control_activity_registrations(assessment),
                ],
            )
            docintel_worker = build_worker(
                self.temporal.client,
                self.config,
                role="docintel",
                activities=docintel_activity_registrations(docintel),
            )
            effects_worker = build_worker(
                self.temporal.client,
                self.config,
                role="effects",
                activities=[
                    *intake_effect_activity_registrations(intake),
                    *assessment_effect_activity_registrations(assessment),
                ],
            )
            return control_worker, docintel_worker, effects_worker

        self.worker, self.docintel_worker, self.effects_worker = self.run(_workers())
        self.run(self.worker.__aenter__())
        self.run(self.docintel_worker.__aenter__())
        self.run(self.effects_worker.__aenter__())
        self.domain.app.state.temporal_chase_driver = self
        return self

    def realtime(self):
        if self.temporal is None:
            raise RuntimeError("Temporal driver has not started")
        return self.temporal.auto_time_skipping_disabled()

    def drain(self) -> ControlResult:
        """Run bounded production drain waves around asynchronous agent Tasks.

        A drain can start or signal the chase Workflow and return before its
        Activity emits the next events. Missing delivery rows therefore cannot
        prove that the outbox is idle. Fixed later waves preserve the real
        workflow boundary while allowing those events, and their synchronous
        downstream emissions, to be consumed.
        """

        if self.temporal is None:
            raise RuntimeError("Temporal driver has not started")

        async def _drain_waves() -> ControlResult:
            result = ControlResult(status="completed")
            for wave in range(_DRAIN_WAVES):
                result = await self.temporal.client.execute_workflow(
                    OutboxDrainWorkflow.run,
                    id=str(agent_workflow_ref(new_ulid())),
                    task_queue=self.config.task_queue("control"),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
                if wave + 1 < _DRAIN_WAVES:
                    await asyncio.sleep(_TASK_SETTLE_SECONDS)
            return result

        with self.realtime():
            return self.run(_drain_waves())

    def settle(self) -> None:
        """Wait in real time for Tasks already released by the test server."""

        async def _settle() -> None:
            for _ in range(100):
                await asyncio.sleep(0.02)

        with self.realtime():
            self.run(_settle())

    def advance_to(self, target: datetime) -> None:
        """Advance both authoritative clocks by the same positive duration."""

        if self.temporal is None:
            raise RuntimeError("Temporal driver has not started")
        duration = target - self.domain.clock.now
        if duration.total_seconds() < 0:
            raise ValueError("Temporal acceptance time cannot move backwards")
        self.domain.clock.advance_to(target)
        self.run(self.temporal.sleep(duration))
        self.settle()

    def close(self) -> None:
        if self.temporal is None:
            return
        try:
            if self.effects_worker is not None:
                self.run(self.effects_worker.__aexit__(None, None, None))
            if self.docintel_worker is not None:
                self.run(self.docintel_worker.__aexit__(None, None, None))
            if self.worker is not None:
                self.run(self.worker.__aexit__(None, None, None))
            self.run(self.temporal.__aexit__(None, None, None))
        finally:
            if hasattr(self.domain.app.state, "temporal_chase_driver"):
                delattr(self.domain.app.state, "temporal_chase_driver")
            self.temporal = None
            asyncio.set_event_loop(None)
            self.loop.close()
