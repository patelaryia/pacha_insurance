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

from sqlalchemy import text
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment

from chase_agent import chase_activity_registrations
from claim_core.service import new_ulid
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


class ChaseTemporalDriver:
    """Run an acceptance-test application through the real chase Workflow."""

    def __init__(self, domain: Any) -> None:
        self.domain = domain
        self.config = local_config()
        self.loop = asyncio.new_event_loop()
        self.temporal: WorkflowEnvironment | None = None
        self.worker = None

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
        self.domain.app.state.dispatcher.register_consumer(
            "temporal_intent",
            TemporalIntentConsumer(starter, TEMPORAL_INTENT_MAPPINGS),
        )
        system = SystemActivities(self.domain.app)
        chase = self.domain.app.state.chase_agent.temporal_activities(
            worker_build_id=self.config.build_id
        )
        async def _worker():
            return build_worker(
                self.temporal.client,
                self.config,
                role="control",
                workflows=[OutboxDrainWorkflow, DocumentChaseWorkflow],
                activities=[
                    system.dispatch_nonledger_events,
                    *chase_activity_registrations(chase),
                ],
            )

        self.worker = self.run(_worker())
        self.run(self.worker.__aenter__())
        self.domain.app.state.temporal_chase_driver = self
        return self

    def realtime(self):
        if self.temporal is None:
            raise RuntimeError("Temporal driver has not started")
        return self.temporal.auto_time_skipping_disabled()

    def drain(self) -> ControlResult:
        """Run finite outbox Workflows until non-ledger delivery is stably idle."""

        if self.temporal is None:
            raise RuntimeError("Temporal driver has not started")

        async def _drain_once() -> ControlResult:
            result = await self.temporal.client.execute_workflow(
                OutboxDrainWorkflow.run,
                id=str(agent_workflow_ref(new_ulid())),
                task_queue=self.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            await asyncio.sleep(0.05)
            return result

        idle_rounds = 0
        result = ControlResult(status="completed")
        for _ in range(16):
            with self.realtime():
                result = self.run(_drain_once())
            with self.domain.app.state.engine.connect() as connection:
                pending = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM event_deliveries "
                        "WHERE consumer != 'ledger' AND status = 'pending'"
                    )
                ).scalar_one()
            idle_rounds = idle_rounds + 1 if pending == 0 else 0
            if idle_rounds == 2:
                return result
        raise AssertionError("non-ledger outbox did not become idle")

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
            if self.worker is not None:
                self.run(self.worker.__aexit__(None, None, None))
            self.run(self.temporal.__aexit__(None, None, None))
        finally:
            if hasattr(self.domain.app.state, "temporal_chase_driver"):
                delattr(self.domain.app.state, "temporal_chase_driver")
            self.temporal = None
            asyncio.set_event_loop(None)
            self.loop.close()
