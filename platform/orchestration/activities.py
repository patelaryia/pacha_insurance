"""System Activities: the bounded edge between Temporal and Pacha services.

Every Activity here wraps an *existing* idempotent Pacha service. None of them
reimplements dispatch, SLA evaluation or ledger appending, and none of them
opens a Temporal client: an Activity that could start Workflows would be a
second, unreviewable orchestration path.

Two rules shape the signatures. First, all four `SystemActivities` take no
arguments and return a `ControlResult`, so no claim fact, event payload, ledger
row or verification hash can reach Workflow history — there is no field for one.
Second, every failure that escapes is a sanitised classification from the closed
§12 taxonomy; the real diagnostic goes to the process log, where it is subject
to Pacha's own retention rather than Temporal's.

The services are synchronous SQLAlchemy, so each call is moved off the event
loop with `asyncio.to_thread`. The one exception is `dispatch_once_async`, which
is already a coroutine because the Temporal bridge consumer it drives has to
await an SDK acknowledgement.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from temporalio import activity

from agent_runtime.projection import (
    AgentRunConflict,
    AgentRunNotFound,
    AgentRunProjection,
)
from orchestration.contracts import ControlCommand, ControlResult
from orchestration.errors import sanitised_application_error

__all__ = [
    "AgentRunActivities",
    "RecurringActivities",
    "SystemActivities",
    "control_activity_registrations",
    "ledger_activity_registrations",
    "recurring_activity_registrations",
]

LOGGER = logging.getLogger(__name__)

#: §16 — one batch. Ten of these is one finite drain execution.
DISPATCH_BATCH_SIZE = 50

#: The consumer whose deliveries belong to the single-writer ledger queue only.
LEDGER_CONSUMER = "ledger"

#: The dispatcher consumer name the Temporal bridge must be registered under.
TEMPORAL_INTENT_CONSUMER = "temporal_intent"


def _batch_status(attempted: int) -> str:
    """`running` when the batch filled, `completed` when the backlog emptied."""

    return "running" if attempted >= DISPATCH_BATCH_SIZE else "completed"


class SystemActivities:
    """The four finite system Activities, bound to one application's services."""

    def __init__(self, app: Any) -> None:
        for dependency in ("dispatcher", "sla_engine", "ledger"):
            if not hasattr(app.state, dependency):
                raise RuntimeError(
                    f"SystemActivities requires app.state.{dependency}; "
                    "build the ordinary Pacha application first"
                )
        dispatcher = app.state.dispatcher
        if TEMPORAL_INTENT_CONSUMER not in dispatcher.consumer_names:
            # Constructing the Worker without the bridge registered would give a
            # green drain that silently starts and Signals nothing at all.
            raise RuntimeError(
                f"register the TemporalIntentConsumer as dispatcher consumer "
                f"{TEMPORAL_INTENT_CONSUMER!r} before constructing SystemActivities"
            )
        self._app = app
        self._dispatcher = dispatcher
        self._sla_engine = app.state.sla_engine
        self._ledger = app.state.ledger

    @activity.defn(name="dispatch_nonledger_events")
    async def dispatch_nonledger_events(self) -> ControlResult:
        """Drive one batch across every consumer except `ledger`.

        This is the Activity that moves the Temporal bridge: `temporal_intent`
        is one of the consumers drained here, alongside the existing domain
        projection consumers. A per-consumer error is already persisted as
        delivery state by the dispatcher and deliberately does not leak into
        Workflow history — the drain's job is throughput, not adjudication.
        """

        consumers = sorted(self._dispatcher.consumer_names - {LEDGER_CONSUMER})
        try:
            attempted = await self._dispatcher.dispatch_once_async(
                consumers, limit=DISPATCH_BATCH_SIZE
            )
        except Exception:
            LOGGER.exception("outbox dispatch failed outside consumer isolation")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status=_batch_status(attempted))

    @activity.defn(name="append_ledger_batch")
    async def append_ledger_batch(self) -> ControlResult:
        """Drive one batch of the `ledger` consumer, and only that consumer.

        Registered on the ledger Task Queue alone, whose Worker concurrency is
        one. `LedgerWriter.consume` is never called directly from anywhere else:
        the dispatcher's delivery rows are what make the append idempotent.
        """

        try:
            attempted = await asyncio.to_thread(
                self._dispatcher.dispatch_once, [LEDGER_CONSUMER], limit=DISPATCH_BATCH_SIZE
            )
        except Exception:
            LOGGER.exception("ledger dispatch failed outside consumer isolation")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status=_batch_status(attempted))

    @activity.defn(name="evaluate_slas")
    async def evaluate_slas(self) -> ControlResult:
        """Run one SLA evaluation pass.

        No time is passed through Workflow history. The engine reads the
        authoritative injected application clock, so a replayed history cannot
        re-evaluate against a stale wall-clock value.
        """

        try:
            await asyncio.to_thread(self._sla_engine.evaluate)
        except Exception:
            LOGGER.exception("SLA evaluation failed")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status="completed")

    @activity.defn(name="verify_ledger")
    async def verify_ledger(self) -> ControlResult:
        """Verify the audit chain and fail visibly when it has diverged.

        An unhealthy report has already entered audit-degraded mode and emitted
        `ops.alert` inside the service, so this Activity's only remaining job is
        to make the scheduled Workflow fail where operations will see it —
        without repeating a hash, a sequence number or any row detail.
        """

        try:
            report = await asyncio.to_thread(self._ledger.run_nightly_verification)
        except Exception:
            LOGGER.exception("nightly ledger verification failed")
            raise sanitised_application_error("activity_internal") from None
        if not report.get("ok"):
            LOGGER.error("audit chain verification reported divergence; audit-degraded mode is on")
            raise sanitised_application_error("payload_diverged")
        return ControlResult(status="completed")


class AgentRunActivities:
    """Projection-synchronising Activities for the `agent_runs` table."""

    def __init__(self, projection: AgentRunProjection, *, worker_build_id: str) -> None:
        if not isinstance(projection, AgentRunProjection):
            raise RuntimeError("AgentRunActivities requires an AgentRunProjection")
        if not isinstance(worker_build_id, str) or not worker_build_id:
            raise RuntimeError("AgentRunActivities requires the deployed Worker build id")
        self._projection = projection
        self._worker_build_id = worker_build_id

    @activity.defn(name="record_agent_run_started")
    async def record_agent_run_started(self, command: ControlCommand) -> ControlResult:
        """Record the execution the run is actually on, from `activity.info()`.

        The Workflow ID, Run ID and type are read from the Activity's own info
        rather than taken from the command: those are what Temporal knows to be
        true, and verifying them against the row is how a diverged projection is
        detected instead of overwritten.
        """

        info = activity.info()
        try:
            await asyncio.to_thread(
                self._projection.record_started,
                run_ref=command.run_ref,
                workflow_ref=info.workflow_id,
                workflow_run_ref=info.workflow_run_id,
                workflow_type=info.workflow_type,
                worker_build_id=self._worker_build_id,
            )
        except AgentRunNotFound:
            LOGGER.exception("agent run projection row is missing")
            raise sanitised_application_error("domain_rejected") from None
        except AgentRunConflict:
            LOGGER.exception("agent run projection conflicts with the observed execution")
            raise sanitised_application_error("idempotency_conflict") from None
        except Exception:
            LOGGER.exception("agent run start projection failed")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status="running", run_ref=command.run_ref)

    @activity.defn(name="record_agent_run_status")
    async def record_agent_run_status(self, result: ControlResult) -> ControlResult:
        """Apply one control-only status observation to the projection row."""

        if result.run_ref is None:
            LOGGER.error("agent run status projection called without a run reference")
            raise sanitised_application_error("domain_rejected")
        try:
            await asyncio.to_thread(self._projection.record_status, result)
        except AgentRunNotFound:
            LOGGER.exception("agent run projection row is missing")
            raise sanitised_application_error("domain_rejected") from None
        except AgentRunConflict:
            LOGGER.exception("agent run projection refused an illegal transition")
            raise sanitised_application_error("idempotency_conflict") from None
        except Exception:
            LOGGER.exception("agent run status projection failed")
            raise sanitised_application_error("activity_internal") from None
        return result


class RecurringActivities:
    """Five control-only adapters over already-idempotent recurring services."""

    def __init__(self, app: Any) -> None:
        for dependency in (
            "notify",
            "graph_integration",
            "eval_harness",
            "projection_agent",
        ):
            if not hasattr(app.state, dependency):
                raise RuntimeError(f"recurring service is not installed: {dependency}")
        graph = app.state.graph_integration
        if not all(
            callable(value)
            for value in (
                getattr(graph.inbound, "delta_once", None),
                getattr(graph.inbound, "renew_once", None),
                getattr(graph.outbound, "release_due", None),
            )
        ):
            raise RuntimeError("GRAPH_SERVICE_NOT_INSTALLED")
        self._app = app

    @activity.defn(name="notify_digest")
    async def notify_digest(self) -> ControlResult:
        try:
            await asyncio.to_thread(
                self._app.state.notify.run_digest, self._app.state.clock()
            )
        except Exception:
            LOGGER.exception("notification digest failed")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status="completed")

    @activity.defn(name="graph_delta_and_release")
    async def graph_delta_and_release(self) -> ControlResult:
        graph = self._app.state.graph_integration
        try:
            delta = await asyncio.to_thread(graph.inbound.delta_once)
            released = await asyncio.to_thread(
                graph.outbound.release_due, self._app.state.clock()
            )
        except Exception:
            LOGGER.exception("Graph delta or outbound release failed")
            raise sanitised_application_error("activity_internal") from None
        if delta.get("status") != "completed" or released.get("status") != "completed":
            raise sanitised_application_error("domain_rejected")
        return ControlResult(status="completed")

    @activity.defn(name="graph_renewal")
    async def graph_renewal(self) -> ControlResult:
        try:
            result = await asyncio.to_thread(
                self._app.state.graph_integration.inbound.renew_once
            )
        except Exception:
            LOGGER.exception("Graph subscription renewal failed")
            raise sanitised_application_error("activity_internal") from None
        if result.get("status") != "completed":
            raise sanitised_application_error("domain_rejected")
        return ControlResult(status="completed")

    @activity.defn(name="weekly_evaluation")
    async def weekly_evaluation(self) -> ControlResult:
        try:
            await asyncio.to_thread(
                self._app.state.eval_harness.corpus.run_weekly,
                actor="agent:eval",
            )
        except Exception:
            LOGGER.exception("weekly evaluation corpus failed")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status="completed")

    @activity.defn(name="paste_readback_sample")
    async def paste_readback_sample(self) -> ControlResult:
        try:
            await asyncio.to_thread(
                self._app.state.projection_agent.sample_paste_readbacks
            )
        except Exception:
            LOGGER.exception("paste readback sampling failed")
            raise sanitised_application_error("activity_internal") from None
        return ControlResult(status="completed")


def control_activity_registrations(
    system: SystemActivities,
    agent_runs: AgentRunActivities,
) -> tuple[Callable[..., Any], ...]:
    """Exactly what a control Worker registers, alongside `SYSTEM_WORKFLOWS`."""

    return (
        system.dispatch_nonledger_events,
        system.evaluate_slas,
        agent_runs.record_agent_run_started,
        agent_runs.record_agent_run_status,
    )


def ledger_activity_registrations(system: SystemActivities) -> tuple[Callable[..., Any], ...]:
    """Exactly what a ledger Worker registers. It registers no Workflow.

    Registering these on the control queue too would defeat the single-writer
    rule the ledger role's concurrency of one exists to enforce, so it is not
    done even as a test shortcut.
    """

    return (system.append_ledger_batch, system.verify_ledger)


def recurring_activity_registrations(
    recurring: RecurringActivities,
) -> tuple[Callable[..., Any], ...]:
    """Exactly the recurring adapters registered on the control queue."""

    return (
        recurring.notify_digest,
        recurring.graph_delta_and_release,
        recurring.graph_renewal,
        recurring.weekly_evaluation,
        recurring.paste_readback_sample,
    )
