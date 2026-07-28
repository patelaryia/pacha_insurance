"""The finite PRD-06 ``DocumentChaseWorkflow``.

Only opaque references, timestamps, hashes and registered control tokens enter
history.  All claim/checklist reads and every governed effect are Activities.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import VersioningBehavior
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from orchestration.contracts import ControlCommand, ControlResult, ControlSignal
    from orchestration.errors import sanitised_application_error
    from orchestration.ids import parse_workflow_ref
    from orchestration.policies import load_retry_policies

__all__ = ["CHASE_WORKFLOWS", "DocumentChaseWorkflow"]

_STEP_INITIAL = "chase_initial_request"
_STEP_WAIT = "chase_wait"
_STEP_REMINDER = "chase_reminder"
_STEP_EXHAUSTED = "chase_exhausted"
_STEP_TERMINAL = "chase_terminal"
# Operational history-safety ceiling. Register #289 records the absence of a
# master-plan value; this ceiling changes no business cadence or reminder cap.
_CONTINUE_AFTER_EVENTS = 64


async def _activity(
    name: str,
    command: ControlCommand,
    *,
    policy_name: str = "db_control",
) -> ControlResult:
    policy = load_retry_policies()[policy_name]
    return await workflow.execute_activity(
        name,
        command,
        result_type=ControlResult,
        start_to_close_timeout=policy.start_to_close_timeout,
        retry_policy=policy.retry_policy,
    )


async def _governed_activity(command: ControlCommand) -> ControlResult:
    """Run one write attempt, converting any lost outcome into human review."""

    try:
        return await _activity(
            "governed_chase_send",
            command,
            policy_name="governed_external_write",
        )
    except ActivityError:
        return await _activity("create_chase_exception", command)


@workflow.defn(
    name="DocumentChaseWorkflow",
    versioning_behavior=VersioningBehavior.PINNED,
)
class DocumentChaseWorkflow:
    """One durable, de-duplicated execution per chase checklist."""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._seen: set[str] = set()
        self._applied_since_continue = 0
        self._last_event_seq = 0
        self._state = ControlResult(status="pending")

    def _enqueue(self, signal: ControlSignal) -> None:
        if signal.event_ref in self._seen:
            return
        self._seen.add(signal.event_ref)
        self._pending.append(signal.event_ref)

    @workflow.signal(name="pacha_event")
    def pacha_event(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="review_resolved")
    def review_resolved(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="claim_terminal")
    def claim_terminal(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="document_received")
    def document_received(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="snooze_changed")
    def snooze_changed(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.signal(name="inbound_received")
    def inbound_received(self, signal: ControlSignal) -> None:
        self._enqueue(signal)

    @workflow.query(name="state")
    def state(self) -> ControlResult:
        return self._state

    async def _apply(
        self,
        command: ControlCommand,
        checklist_ref: str,
        event_ref: str | None,
    ) -> None:
        try:
            self._state = await _activity(
                "apply_chase_event",
                ControlCommand(
                    run_ref=command.run_ref,
                    claim_ref=command.claim_ref,
                    checklist_ref=checklist_ref,
                    event_ref=event_ref,
                    step_id="chase_apply_event",
                ),
            )
        except ActivityError as error:
            cause = error.cause
            if not (
                isinstance(cause, ApplicationError)
                and cause.type == "domain_rejected"
            ):
                raise
            # A forged, stale or unauthorised Signal is rejected by the
            # Activity and cannot kill the valid chase execution.
            self._state = ControlResult(status="running", step_id=_STEP_WAIT)
            return
        if (
            event_ref is None
            or self._state.event_seq is None
            or self._state.event_seq <= self._last_event_seq
        ):
            return
        self._last_event_seq = self._state.event_seq
        self._applied_since_continue += 1
        if self._applied_since_continue < _CONTINUE_AFTER_EVENTS or self._pending:
            return
        workflow.continue_as_new(
            ControlCommand(
                run_ref=command.run_ref,
                claim_ref=command.claim_ref,
                checklist_ref=checklist_ref,
                trigger_event_ref=command.trigger_event_ref,
                event_ref=event_ref,
                event_seq=self._state.event_seq,
            )
        )

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        kind, checklist_ref = parse_workflow_ref(workflow.info().workflow_id)
        if kind != "chase":
            raise sanitised_application_error("domain_rejected")
        self._last_event_seq = command.event_seq or 0

        base = ControlCommand(
            run_ref=command.run_ref,
            claim_ref=command.claim_ref,
            checklist_ref=checklist_ref,
            trigger_event_ref=command.trigger_event_ref,
            event_ref=command.event_ref,
        )
        self._state = await _activity("record_chase_started", base)

        while True:
            state = await _activity(
                "load_chase_state",
                ControlCommand(
                    run_ref=command.run_ref,
                    claim_ref=command.claim_ref,
                    checklist_ref=checklist_ref,
                ),
            )
            self._state = state

            if (
                state.step_id == _STEP_TERMINAL
                or state.status in {"completed", "cancelled", "blocked"}
            ):
                self._state = await _activity(
                    "record_chase_terminal",
                    ControlCommand(
                        run_ref=command.run_ref,
                        claim_ref=command.claim_ref,
                        checklist_ref=checklist_ref,
                        event_ref=state.event_ref,
                        step_id=_STEP_TERMINAL,
                    ),
                )
                return self._state

            if state.status == "awaiting_review":
                await workflow.wait_condition(lambda: bool(self._pending))
                await self._apply(command, checklist_ref, self._pending.pop(0))
                continue

            if state.step_id == _STEP_INITIAL:
                sent = await _governed_activity(
                    ControlCommand(
                        run_ref=command.run_ref,
                        claim_ref=command.claim_ref,
                        checklist_ref=checklist_ref,
                        write_id=f"chase:{checklist_ref.lower()}:0",
                        step_id=_STEP_INITIAL,
                    ),
                )
                self._state = sent
                if sent.status == "cancelled":
                    self._state = await _activity(
                        "record_chase_terminal",
                        ControlCommand(
                            run_ref=command.run_ref,
                            claim_ref=command.claim_ref,
                            checklist_ref=checklist_ref,
                            event_ref=sent.event_ref,
                            step_id=_STEP_TERMINAL,
                        ),
                    )
                    return self._state
                continue

            if state.step_id == _STEP_EXHAUSTED:
                if state.status != "awaiting_review":
                    state = await _activity(
                        "create_chase_exception",
                        ControlCommand(
                            run_ref=command.run_ref,
                            claim_ref=command.claim_ref,
                            checklist_ref=checklist_ref,
                            event_ref=state.event_ref,
                            step_id=_STEP_EXHAUSTED,
                        ),
                    )
                    self._state = state
                if state.status != "awaiting_review":
                    continue
                continue

            if state.step_id == _STEP_REMINDER:
                reminder_index = state.attempt_no
                if reminder_index is None:
                    raise sanitised_application_error("activity_internal")
                sent = await _governed_activity(
                    ControlCommand(
                        run_ref=command.run_ref,
                        claim_ref=command.claim_ref,
                        checklist_ref=checklist_ref,
                        write_id=f"chase:{checklist_ref.lower()}:{reminder_index}",
                        step_id=_STEP_REMINDER,
                    ),
                )
                self._state = sent
                if sent.status == "cancelled":
                    self._state = await _activity(
                        "record_chase_terminal",
                        ControlCommand(
                            run_ref=command.run_ref,
                            claim_ref=command.claim_ref,
                            checklist_ref=checklist_ref,
                            event_ref=sent.event_ref,
                            step_id=_STEP_TERMINAL,
                        ),
                    )
                    return self._state
                continue

            if state.step_id != _STEP_WAIT:
                raise sanitised_application_error("activity_internal")

            event_ref: str | None = None
            if self._pending:
                event_ref = self._pending.pop(0)
            elif state.wake_at_epoch_ms is None:
                await workflow.wait_condition(lambda: bool(self._pending))
                event_ref = self._pending.pop(0)
            else:
                now_epoch_ms = int(workflow.now().timestamp() * 1000)
                wait_ms = max(0, state.wake_at_epoch_ms - now_epoch_ms)
                try:
                    await workflow.wait_condition(
                        lambda: bool(self._pending),
                        timeout=timedelta(milliseconds=wait_ms),
                    )
                except TimeoutError:
                    event_ref = None
                else:
                    event_ref = self._pending.pop(0)

            await self._apply(command, checklist_ref, event_ref)


CHASE_WORKFLOWS: tuple[type, ...] = (DocumentChaseWorkflow,)
