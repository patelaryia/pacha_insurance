"""The finite, pinned PRD-01 document-intelligence Workflow."""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import VersioningBehavior
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlCommand, ControlResult, ControlSignal
    from orchestration.errors import sanitised_application_error
    from orchestration.ids import parse_workflow_ref
    from orchestration.policies import load_retry_policies

__all__ = ["DOCINTEL_WORKFLOWS", "DocumentIntelligenceWorkflow"]

_ACTIVITIES: tuple[tuple[str, str], ...] = (
    ("docintel_normalize", "long_compute"),
    ("docintel_classify", "provider_managed_retry"),
    ("docintel_split", "provider_managed_retry"),
    ("docintel_extract", "provider_managed_retry"),
    ("docintel_cite", "provider_managed_retry"),
    ("docintel_validate", "long_compute"),
    ("docintel_commit", "db_control"),
    ("docintel_consistency", "provider_managed_retry"),
)
_CONTROL_QUEUE_SUFFIX = "-control-v1"
_DOCINTEL_QUEUE_SUFFIX = "-docintel-v1"


def _docintel_task_queue() -> str:
    """Derive the sibling role queue from the configured control queue."""

    control_queue = workflow.info().task_queue
    if not control_queue.endswith(_CONTROL_QUEUE_SUFFIX):
        raise sanitised_application_error("domain_rejected")
    return f"{control_queue.removesuffix(_CONTROL_QUEUE_SUFFIX)}{_DOCINTEL_QUEUE_SUFFIX}"


@workflow.defn(
    name="DocumentIntelligenceWorkflow",
    versioning_behavior=VersioningBehavior.PINNED,
)
class DocumentIntelligenceWorkflow:
    """One execution per document ULID, with database stages as restart truth."""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._seen: set[str] = set()
        self._state = ControlResult(status="pending")

    @workflow.signal(name="pacha_event")
    def pacha_event(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="review_resolved")
    def review_resolved(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    def _enqueue(self, signal: ControlSignal) -> None:
        if signal.event_ref in self._seen:
            return
        self._seen.add(signal.event_ref)
        self._pending.append(signal.event_ref)

    @workflow.query(name="state")
    def state(self) -> ControlResult:
        return self._state

    async def _stage(
        self,
        *,
        name: str,
        policy_name: str,
        command: ControlCommand,
    ) -> ControlResult:
        policy = load_retry_policies()[policy_name]
        heartbeat_timeout = load_retry_policies()["long_compute"].heartbeat_timeout
        try:
            return await workflow.execute_activity(
                name,
                command,
                result_type=ControlResult,
                task_queue=_docintel_task_queue(),
                start_to_close_timeout=policy.start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=policy.retry_policy,
            )
        except ActivityError:
            return ControlResult(
                status="blocked",
                run_ref=command.run_ref,
                attempt_no=command.attempt_no,
            )

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        kind, document_ref = parse_workflow_ref(workflow.info().workflow_id)
        if (
            kind != "docintel"
            or (
                command.document_ref is not None
                and command.document_ref != document_ref
            )
        ):
            raise sanitised_application_error("domain_rejected")

        for checkpoint, (name, policy_name) in enumerate(_ACTIVITIES, start=1):
            stage_command = ControlCommand(
                run_ref=command.run_ref,
                document_ref=document_ref,
                attempt_no=checkpoint,
            )
            while True:
                self._state = await self._stage(
                    name=name,
                    policy_name=policy_name,
                    command=stage_command,
                )
                if self._state.status == "running":
                    break
                if self._state.status == "completed":
                    return self._state
                await workflow.wait_condition(lambda: bool(self._pending))
                self._pending.pop(0)

        return self._state


DOCINTEL_WORKFLOWS: tuple[type, ...] = (DocumentIntelligenceWorkflow,)
