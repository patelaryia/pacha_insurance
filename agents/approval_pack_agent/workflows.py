"""Pinned, control-only Temporal orchestration for PRD-08 approval packs."""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlCommand, ControlResult, ControlSignal
    from orchestration.errors import sanitised_application_error
    from orchestration.ids import parse_workflow_ref
    from orchestration.policies import load_retry_policies

__all__ = ["APPROVAL_PACK_WORKFLOWS", "ApprovalPackWorkflow"]

_ACTIVITIES = (
    "approval_resolve_manifest",
    "approval_merge",
    "approval_generate_note",
    "approval_grade_and_queue",
    "approval_apply_review",
    "approval_prepare_signature",
    "approval_finalize_signature",
    "approval_record_terminal",
)
_SINGLE_ATTEMPT = frozenset(
    {
        "approval_generate_note",
        "approval_prepare_signature",
        "approval_finalize_signature",
    }
)
_CONTROL_QUEUE_SUFFIX = "-control-v1"


def _control_queue() -> str:
    queue = workflow.info().task_queue
    if not queue.endswith(_CONTROL_QUEUE_SUFFIX):
        raise sanitised_application_error("domain_rejected")
    return queue


@workflow.defn(name="ApprovalPackWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class ApprovalPackWorkflow:
    """One durable approval-pack execution per precommitted agent-run ULID."""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._seen: set[str] = set()
        self._state = ControlResult(status="pending")

    @workflow.signal(name="review_resolved")
    def review_resolved(self, signal: ControlSignal) -> None:
        if signal.event_ref not in self._seen:
            self._seen.add(signal.event_ref)
            self._pending.append(signal.event_ref)

    @workflow.query(name="state")
    def state(self) -> ControlResult:
        return self._state

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        kind, run_ref = parse_workflow_ref(workflow.info().workflow_id)
        if kind != "approval-pack" or command.run_ref != run_ref:
            raise sanitised_application_error("domain_rejected")

        policies = load_retry_policies()
        for attempt_no, activity_name in enumerate(_ACTIVITIES, start=1):
            while True:
                current = ControlCommand(
                    run_ref=run_ref,
                    claim_ref=command.claim_ref,
                    trigger_event_ref=command.trigger_event_ref,
                    event_ref=self._pending[-1] if self._pending else command.event_ref,
                    attempt_no=attempt_no,
                )
                policy = (
                    policies["provider_managed_retry"]
                    if activity_name in _SINGLE_ATTEMPT
                    else policies["db_control"]
                )
                try:
                    self._state = await workflow.execute_activity(
                        activity_name,
                        current,
                        result_type=ControlResult,
                        task_queue=_control_queue(),
                        start_to_close_timeout=policy.start_to_close_timeout,
                        retry_policy=(
                            RetryPolicy(maximum_attempts=1)
                            if activity_name in _SINGLE_ATTEMPT
                            else policy.retry_policy
                        ),
                    )
                except ActivityError:
                    return ControlResult(
                        status="blocked",
                        run_ref=run_ref,
                        claim_ref=command.claim_ref,
                        attempt_no=attempt_no,
                    )
                if self._state.status == "running":
                    break
                if self._state.status in {
                    "blocked",
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return self._state
                await workflow.wait_condition(lambda: bool(self._pending))
                self._pending.pop(0)
        return ControlResult(
            status="completed",
            run_ref=run_ref,
            claim_ref=command.claim_ref,
            attempt_no=len(_ACTIVITIES),
        )


APPROVAL_PACK_WORKFLOWS: tuple[type, ...] = (ApprovalPackWorkflow,)
