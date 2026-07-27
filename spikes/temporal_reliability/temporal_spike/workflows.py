"""Representative durable workflow and its replay-compatible predecessor."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .contracts import (
        ControlResult,
        ExternalActionCommand,
        HeartbeatCommand,
        ReviewCommand,
        RunRef,
        StepCommand,
        WorkflowInput,
    )

TECHNICAL_RETRY = RetryPolicy(
    initial_interval=timedelta(milliseconds=100),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=3,
)
HEARTBEAT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=1,
    maximum_interval=timedelta(seconds=2),
    maximum_attempts=3,
)
NO_EXTERNAL_RETRY = RetryPolicy(maximum_attempts=1)


class _WorkflowFlow:
    def __init__(self) -> None:
        self._review_event_ref: str | None = None
        self._stage = "created"

    @workflow.signal
    async def human_review(self, review_event_ref: str) -> None:
        self._review_event_ref = review_event_ref

    @workflow.query
    def stage(self) -> str:
        return self._stage

    async def _execute(self, command: WorkflowInput, *, compatibility_v2: bool) -> str:
        refs = RunRef(command.run_ref, command.claim_ref)
        await workflow.execute_activity(
            "record_started",
            refs,
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )

        self._stage = "automated:prepare"
        await workflow.execute_activity(
            "automated_step",
            StepCommand(command.run_ref, command.claim_ref, "prepare"),
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )

        self._stage = "heartbeating"
        await workflow.execute_activity(
            "heartbeating_step",
            HeartbeatCommand(command.run_ref, command.claim_ref, 3),
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=1),
            retry_policy=HEARTBEAT_RETRY,
        )

        self._stage = "automated:validate"
        await workflow.execute_activity(
            "automated_step",
            StepCommand(command.run_ref, command.claim_ref, "validate"),
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )

        if command.timer_seconds:
            self._stage = "timer"
            await workflow.sleep(timedelta(seconds=command.timer_seconds))

        self._stage = "awaiting_review"
        await workflow.execute_activity(
            "record_awaiting_review",
            refs,
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )
        await workflow.wait_condition(lambda: self._review_event_ref is not None)
        review_event_ref = self._review_event_ref
        assert review_event_ref is not None
        await workflow.execute_activity(
            "apply_review",
            ReviewCommand(command.run_ref, review_event_ref),
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )

        if compatibility_v2 and workflow.patched("pacha-spike-compatibility-v2"):
            await workflow.execute_activity(
                "record_compatibility_marker",
                refs,
                result_type=ControlResult,
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=TECHNICAL_RETRY,
            )

        self._stage = "external_action"
        try:
            await workflow.execute_activity(
                "governed_external_action",
                ExternalActionCommand(
                    command.run_ref,
                    command.claim_ref,
                    f"payload:{command.claim_ref}",
                    command.payload_hash,
                    f"write:{command.run_ref}",
                ),
                result_type=ControlResult,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=NO_EXTERNAL_RETRY,
            )
        except ActivityError:
            await workflow.execute_activity(
                "record_blocked",
                refs,
                result_type=ControlResult,
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=TECHNICAL_RETRY,
            )
            self._stage = "blocked"
            return "blocked"

        await workflow.execute_activity(
            "record_completed",
            refs,
            result_type=ControlResult,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=TECHNICAL_RETRY,
        )
        self._stage = "completed"
        return "completed"


@workflow.defn(name="PachaReliabilityWorkflow")
class DurableClaimWorkflowV1(_WorkflowFlow):
    @workflow.run
    async def run(self, command: WorkflowInput) -> str:
        return await self._execute(command, compatibility_v2=False)


@workflow.defn(name="PachaReliabilityWorkflow")
class DurableClaimWorkflow(_WorkflowFlow):
    @workflow.run
    async def run(self, command: WorkflowInput) -> str:
        return await self._execute(command, compatibility_v2=True)
