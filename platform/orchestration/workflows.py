"""The four finite system Workflows (master plan §16).

This module's import list is the specification. It may reach `temporalio` and
the four deterministic pass-through modules named in
`worker.WORKFLOW_SAFE_MODULES`, and nothing else — no application service, no
SQLAlchemy, no configuration, no client, no Codec and *not* `activities.py`.
Importing the Activity module would drag the dispatcher, the SLA engine and the
ledger into the replay sandbox, so Activities are invoked by their registered
string names instead.

Every Workflow here is finite by construction. The drain loops process at most
ten fifty-row batches and return, which caps one execution at 500 delivery-row
attempts; remaining backlog is the next Schedule execution's work (T07). There
is no timer, no Continue-As-New, no randomness and no wall-clock read, so
replay is exact.

The ledger Task Queue is derived from the queue this Workflow is *actually*
running on rather than read from configuration, because configuration is not
available in a deterministic replay context and a Schedule that moved the
Workflow to a different environment must move its ledger Activities with it.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlResult
    from orchestration.errors import sanitised_application_error
    from orchestration.policies import load_retry_policies

__all__ = [
    "MAX_DRAIN_BATCHES",
    "SYSTEM_WORKFLOWS",
    "LedgerDrainWorkflow",
    "LedgerVerificationWorkflow",
    "OutboxDrainWorkflow",
    "SlaEvaluationWorkflow",
]

#: §16 — at most ten batches per execution, so one execution is bounded at
#: 10 × 50 = 500 delivery-row attempts.
MAX_DRAIN_BATCHES = 10

_CONTROL_QUEUE_SUFFIX = "-control-v1"
_LEDGER_QUEUE_SUFFIX = "-ledger-v1"


def _ledger_task_queue() -> str:
    """`pacha-{env}-control-v1` -> `pacha-{env}-ledger-v1`, or refuse."""

    current = workflow.info().task_queue
    if not current.endswith(_CONTROL_QUEUE_SUFFIX):
        # A system Workflow polled from anywhere but a control queue means the
        # Worker registration is wrong; deriving a ledger queue from it would
        # send ledger appends somewhere unreviewed.
        raise sanitised_application_error("activity_internal")
    return current[: -len(_CONTROL_QUEUE_SUFFIX)] + _LEDGER_QUEUE_SUFFIX


async def _batch(activity_name: str, policy_name: str, *, task_queue: str | None = None):
    """Invoke one bounded batch Activity by its registered name."""

    policy = load_retry_policies()[policy_name]
    return await workflow.execute_activity(
        activity_name,
        result_type=ControlResult,
        task_queue=task_queue,
        start_to_close_timeout=policy.start_to_close_timeout,
        retry_policy=policy.retry_policy,
    )


async def _drain(activity_name: str, policy_name: str, *, task_queue: str | None = None):
    """Run batches until the backlog empties or the ten-batch cap is reached.

    `completed` means the Activity attempted fewer rows than the batch size, so
    the backlog is empty and looping again would be a wasted round trip.
    `running` means the batch filled and more may be waiting. Any other status
    is a contract violation between Workflow and Activity, and guessing what it
    meant is exactly the kind of silent default the guide forbids.
    """

    for _batch_number in range(MAX_DRAIN_BATCHES):
        result = await _batch(activity_name, policy_name, task_queue=task_queue)
        if result.status == "completed":
            break
        if result.status != "running":
            raise sanitised_application_error("activity_internal")
    # Reaching the cap is a normal outcome, not a failure: the next Schedule
    # execution picks up whatever is left.
    return ControlResult(status="completed")


@workflow.defn(name="OutboxDrainWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class OutboxDrainWorkflow:
    """Drain every non-ledger outbox consumer, including the Temporal bridge."""

    @workflow.run
    async def run(self) -> ControlResult:
        return await _drain("dispatch_nonledger_events", "db_control")


@workflow.defn(name="LedgerDrainWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class LedgerDrainWorkflow:
    """Drain the `ledger` consumer only, on the single-writer ledger queue."""

    @workflow.run
    async def run(self) -> ControlResult:
        return await _drain(
            "append_ledger_batch", "ledger_append", task_queue=_ledger_task_queue()
        )


@workflow.defn(name="SlaEvaluationWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class SlaEvaluationWorkflow:
    """One evaluation pass over the durable SLA clocks."""

    @workflow.run
    async def run(self) -> ControlResult:
        return await _batch("evaluate_slas", "db_control")


@workflow.defn(name="LedgerVerificationWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class LedgerVerificationWorkflow:
    """One nightly audit-chain verification, on the ledger queue."""

    @workflow.run
    async def run(self) -> ControlResult:
        return await _batch("verify_ledger", "ledger_append", task_queue=_ledger_task_queue())


#: The exact set a control Worker registers. Deliberately not exported from
#: `orchestration.__init__`: registration is explicit at the Worker call site.
SYSTEM_WORKFLOWS: tuple[type, ...] = (
    OutboxDrainWorkflow,
    LedgerDrainWorkflow,
    SlaEvaluationWorkflow,
    LedgerVerificationWorkflow,
)
