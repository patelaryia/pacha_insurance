"""Finite recurring Workflow wrappers and immutable Schedule bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.common import VersioningBehavior
from temporalio.service import RPCError, RPCStatusCode

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlResult
    from orchestration.policies import load_retry_policies

GRAPH_SERVICE_NOT_INSTALLED = "GRAPH_SERVICE_NOT_INSTALLED"


@workflow.defn(name="NotifyDigestWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class NotifyDigestWorkflow:
    @workflow.run
    async def run(self) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        return await workflow.execute_activity(
            "notify_digest",
            result_type=ControlResult,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@workflow.defn(name="GraphDeltaWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class GraphDeltaWorkflow:
    @workflow.run
    async def run(self) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        return await workflow.execute_activity(
            "graph_delta_and_release",
            result_type=ControlResult,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@workflow.defn(name="GraphRenewalWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class GraphRenewalWorkflow:
    @workflow.run
    async def run(self) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        return await workflow.execute_activity(
            "graph_renewal",
            result_type=ControlResult,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@workflow.defn(name="WeeklyEvaluationWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class WeeklyEvaluationWorkflow:
    @workflow.run
    async def run(self) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        return await workflow.execute_activity(
            "weekly_evaluation",
            result_type=ControlResult,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@workflow.defn(
    name="PasteReadbackSampleWorkflow",
    versioning_behavior=VersioningBehavior.PINNED,
)
class PasteReadbackSampleWorkflow:
    @workflow.run
    async def run(self) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        return await workflow.execute_activity(
            "paste_readback_sample",
            result_type=ControlResult,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    schedule_id: str
    timing: str
    overlap_policy: ScheduleOverlapPolicy
    catchup_window: str
    pause_on_failure: bool
    workflow_name: str


def schedule_definitions(*, env: str, weekly_time: str) -> tuple[ScheduleDefinition, ...]:
    prefix = f"pacha-{env}"
    skip = ScheduleOverlapPolicy.SKIP
    buffered = ScheduleOverlapPolicy.BUFFER_ONE
    return (
        ScheduleDefinition(
            f"{prefix}-outbox-drain-v1", "30s", skip, "5m", False, "OutboxDrainWorkflow"
        ),
        ScheduleDefinition(
            f"{prefix}-ledger-drain-v1", "10s", skip, "5m", False, "LedgerDrainWorkflow"
        ),
        ScheduleDefinition(
            f"{prefix}-sla-evaluate-v1", "5m", skip, "30m", False, "SlaEvaluationWorkflow"
        ),
        ScheduleDefinition(
            f"{prefix}-ledger-verify-v1",
            "01:00 UTC daily",
            buffered,
            "24h",
            False,
            "LedgerVerificationWorkflow",
        ),
        ScheduleDefinition(
            f"{prefix}-notify-digest-v1",
            "05:00 UTC daily",
            buffered,
            "24h",
            False,
            "NotifyDigestWorkflow",
        ),
        ScheduleDefinition(
            f"{prefix}-graph-delta-v1", "60s", skip, "5m", False, "GraphDeltaWorkflow"
        ),
        ScheduleDefinition(
            f"{prefix}-graph-renew-v1",
            "71h",
            buffered,
            "24h",
            False,
            "GraphRenewalWorkflow",
        ),
        ScheduleDefinition(
            f"{prefix}-eval-weekly-v1",
            weekly_time,
            buffered,
            "7d",
            False,
            "WeeklyEvaluationWorkflow",
        ),
        ScheduleDefinition(
            f"{prefix}-paste-readback-v1",
            "Monday 05:00 UTC",
            buffered,
            "24h",
            False,
            "PasteReadbackSampleWorkflow",
        ),
    )


_DURATIONS = {
    "10s": timedelta(seconds=10),
    "30s": timedelta(seconds=30),
    "60s": timedelta(seconds=60),
    "5m": timedelta(minutes=5),
    "30m": timedelta(minutes=30),
    "24h": timedelta(hours=24),
    "71h": timedelta(hours=71),
    "7d": timedelta(days=7),
}


def _spec(timing: str) -> ScheduleSpec:
    if timing in {"10s", "30s", "60s", "5m", "71h"}:
        return ScheduleSpec(intervals=[ScheduleIntervalSpec(every=_DURATIONS[timing])])
    if timing == "01:00 UTC daily":
        return ScheduleSpec(cron_expressions=["0 1 * * *"])
    if timing == "05:00 UTC daily":
        return ScheduleSpec(cron_expressions=["0 5 * * *"])
    if timing == "Monday 05:00 UTC":
        return ScheduleSpec(cron_expressions=["0 5 * * 1"])
    if timing == "pack weekly":
        return ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=7))])
    raise ValueError("weekly schedule time is not a recognised pack value")


def _temporal_schedule(definition: ScheduleDefinition, task_queue: str) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            definition.workflow_name,
            id=f"{definition.schedule_id}-workflow",
            task_queue=task_queue,
        ),
        spec=_spec(definition.timing),
        policy=SchedulePolicy(
            overlap=definition.overlap_policy,
            catchup_window=_DURATIONS[definition.catchup_window],
            pause_on_failure=False,
        ),
    )


def _graph_ready(graph_service: Any) -> bool:
    inbound = getattr(graph_service, "inbound", None)
    outbound = getattr(graph_service, "outbound", None)
    return all(
        callable(value)
        for value in (
            getattr(inbound, "delta_once", None),
            getattr(inbound, "renew_once", None),
            getattr(outbound, "release_due", None),
        )
    )


async def bootstrap_schedules(
    client: Any,
    *,
    env: str,
    weekly_time: str,
    graph_service: Any,
    task_queue: str | None = None,
) -> tuple[str, ...]:
    """Create missing definitions and refuse any existing definition drift."""

    if not _graph_ready(graph_service):
        raise RuntimeError(GRAPH_SERVICE_NOT_INSTALLED)
    queue = task_queue or f"pacha-{env}-control-v1"
    created: list[str] = []
    for definition in schedule_definitions(env=env, weekly_time=weekly_time):
        expected = _temporal_schedule(definition, queue)
        handle = client.get_schedule_handle(definition.schedule_id)
        try:
            description = await handle.describe()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
            await client.create_schedule(definition.schedule_id, expected)
            created.append(definition.schedule_id)
            continue
        if description.schedule != expected:
            raise RuntimeError(f"schedule definition drift: {definition.schedule_id}")
    return tuple(created)


RECURRING_WORKFLOWS: tuple[type, ...] = (
    NotifyDigestWorkflow,
    GraphDeltaWorkflow,
    GraphRenewalWorkflow,
    WeeklyEvaluationWorkflow,
    PasteReadbackSampleWorkflow,
)


__all__ = [
    "GraphDeltaWorkflow",
    "GraphRenewalWorkflow",
    "NotifyDigestWorkflow",
    "PasteReadbackSampleWorkflow",
    "RECURRING_WORKFLOWS",
    "ScheduleDefinition",
    "WeeklyEvaluationWorkflow",
    "bootstrap_schedules",
    "schedule_definitions",
]
