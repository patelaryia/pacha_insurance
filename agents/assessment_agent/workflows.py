"""Pinned, control-only Temporal orchestration for PRD-07 assessment."""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlCommand, ControlResult, ControlSignal
    from orchestration.errors import sanitised_application_error
    from orchestration.ids import parse_workflow_ref
    from orchestration.policies import load_retry_policies

__all__ = ["ASSESSMENT_WORKFLOWS", "AssessmentWorkflow"]

_ACTIVITIES = (
    "assessment_prepare",
    "assessment_mode_shadow",
    "assessment_apply_mode_review",
    "assessment_dispatch",
    "assessment_parse_report",
    "assessment_cascade",
    "assessment_record_terminal",
)
_DISPATCH = "assessment_dispatch"
_CONTROL_QUEUE_SUFFIX = "-control-v1"
_EFFECTS_QUEUE_SUFFIX = "-effects-v1"


def _queue(activity_name: str) -> str:
    control = workflow.info().task_queue
    if not control.endswith(_CONTROL_QUEUE_SUFFIX):
        raise sanitised_application_error("domain_rejected")
    if activity_name == _DISPATCH:
        return control.removesuffix(_CONTROL_QUEUE_SUFFIX) + _EFFECTS_QUEUE_SUFFIX
    return control


@workflow.defn(name="AssessmentWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class AssessmentWorkflow:
    """One execution per precommitted assessment agent-run ULID."""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._seen: set[str] = set()
        self._state = ControlResult(status="pending")

    def _enqueue(self, signal: ControlSignal) -> None:
        if signal.event_ref not in self._seen:
            self._seen.add(signal.event_ref)
            self._pending.append(signal.event_ref)

    @workflow.signal(name="review_resolved")
    def review_resolved(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="document_received")
    def document_received(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="claim_terminal")
    def claim_terminal(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.query(name="state")
    def state(self) -> ControlResult:
        return self._state

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        kind, run_ref = parse_workflow_ref(workflow.info().workflow_id)
        if kind != "assessment" or command.run_ref != run_ref:
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
                    policies["governed_external_write"]
                    if activity_name == _DISPATCH
                    else policies["db_control"]
                )
                try:
                    self._state = await workflow.execute_activity(
                        activity_name,
                        current,
                        result_type=ControlResult,
                        task_queue=_queue(activity_name),
                        start_to_close_timeout=policy.start_to_close_timeout,
                        retry_policy=(
                            RetryPolicy(maximum_attempts=1)
                            if activity_name == _DISPATCH
                            else policy.retry_policy
                        ),
                    )
                except ActivityError:
                    self._state = ControlResult(
                        status="blocked",
                        run_ref=run_ref,
                        attempt_no=attempt_no,
                    )
                if self._state.status == "running":
                    break
                if self._state.status in {"completed", "cancelled"}:
                    return self._state
                await workflow.wait_condition(lambda: bool(self._pending))
                self._pending.pop(0)
        return ControlResult(
            status="completed",
            run_ref=run_ref,
            claim_ref=command.claim_ref,
            attempt_no=len(_ACTIVITIES),
        )


ASSESSMENT_WORKFLOWS: tuple[type, ...] = (AssessmentWorkflow,)
