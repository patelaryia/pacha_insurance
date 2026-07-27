"""The only start and Signal transport, and the outbox intent bridge (§14/§15).

Every Temporal start and Signal Pacha will ever issue goes through
`TemporalStarter`. Concentrating them is what makes the duplicate-start policy,
the control queue and the closed Signal registry reviewable in one place instead
of asserted at every call site — and it is why no method here returns a Workflow
handle. A domain package that could hold a handle could Query it, terminate it
or wait on it, and Temporal is not a read model, an approval authority or the
record of an external write.

`TemporalIntentConsumer` is the dispatcher-side half. It is an ordinary outbox
consumer: the initiating transaction commits its event and returns, and the
delivery is marked succeeded only after the SDK acknowledges. A failure between
those two points therefore leaves the delivery retryable, which is safe because
starts use `USE_EXISTING` and Signals are de-duplicated by event reference in
the receiving Workflow.

Mappings are Python dataclass instances declared in code, not configuration.
Event-to-Workflow routing is system behaviour rather than business cadence, and
a code-owned tuple makes reflection, discovery, wildcards and pack-supplied
Workflow names impossible by construction.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from temporalio.client import Client

from orchestration.config import TemporalConfig
from orchestration.contracts import (
    REQUIRED_ID_CONFLICT_POLICY,
    REQUIRED_ID_REUSE_POLICY,
    ControlCommand,
    ControlSignal,
    validate_control_field,
)
from orchestration.errors import ControlContractError
from orchestration.ids import WorkflowRef, chase_workflow_ref

if TYPE_CHECKING:  # pragma: no cover - typing only; no runtime claim_core import
    from claim_core.models import Event

__all__ = [
    "STANDARD_SIGNAL_NAMES",
    "TEMPORAL_INTENT_MAPPINGS",
    "TemporalIntentConsumer",
    "TemporalIntentMapping",
    "TemporalStarter",
]

#: Master plan §15 — the closed Signal registry. A Signal name outside this set
#: is refused, so a caller cannot invent a channel a Workflow never declared.
STANDARD_SIGNAL_NAMES = frozenset(
    {
        "pacha_event",
        "review_resolved",
        "claim_terminal",
        "document_received",
        "snooze_changed",
        "inbound_received",
    }
)

#: Temporal type names are code-owned registry tokens, never prose or a wildcard.
_WORKFLOW_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,159}$")


class TemporalStarter:
    """Start Workflows and deliver Signals. Nothing else crosses this edge."""

    def __init__(self, client: Client, config: TemporalConfig) -> None:
        self._client = client
        self._config = config

    async def start(
        self,
        *,
        workflow_type: str | type,
        workflow_ref: WorkflowRef,
        command: ControlCommand,
    ) -> None:
        """Start one Workflow on the control queue and return once acknowledged.

        Both duplicate policies are set explicitly on every start rather than
        left to an SDK default: `REJECT_DUPLICATE` plus `USE_EXISTING` is what
        makes a retried delivery attach to the execution the already-committed
        Pacha ULID identifies, instead of duplicating domain work.

        Returns once the SDK acknowledges the start. It never waits for the
        Workflow to finish, and it never returns the handle.
        """

        if not isinstance(workflow_ref, WorkflowRef):
            raise ControlContractError("workflow_ref", "must be an orchestration.ids.WorkflowRef")
        if not isinstance(command, ControlCommand):
            raise ControlContractError("command", "a start carries exactly one ControlCommand")

        await self._client.start_workflow(
            workflow_type,
            command,
            id=workflow_ref.workflow_ref,
            task_queue=self._config.task_queue("control"),
            id_reuse_policy=REQUIRED_ID_REUSE_POLICY,
            id_conflict_policy=REQUIRED_ID_CONFLICT_POLICY,
        )

    async def signal(
        self,
        *,
        workflow_ref: WorkflowRef,
        signal_name: str,
        signal: ControlSignal,
    ) -> None:
        """Deliver one opaque event reference to an existing execution."""

        if not isinstance(workflow_ref, WorkflowRef):
            raise ControlContractError("workflow_ref", "must be an orchestration.ids.WorkflowRef")
        if signal_name not in STANDARD_SIGNAL_NAMES:
            raise ControlContractError("signal_name", "not a standard Pacha Signal name")
        if not isinstance(signal, ControlSignal):
            raise ControlContractError("signal", "a Signal carries exactly one ControlSignal")

        handle = self._client.get_workflow_handle(workflow_ref.workflow_ref)
        await handle.signal(signal_name, signal)


@dataclass(frozen=True, slots=True)
class TemporalIntentMapping:
    """One committed event type routed to one Workflow start or Signal."""

    event_type: str
    workflow_type: str | type
    workflow_id_builder: Callable[[Event], WorkflowRef | None]
    action: Literal["start", "signal"]
    signal_name: str | None
    control_contract_type: type[ControlCommand] | type[ControlSignal]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_type, str)
            or not self.event_type
            or self.event_type != self.event_type.strip()
        ):
            raise ControlContractError("event_type", "must be a non-empty event type")
        if isinstance(self.workflow_type, str):
            if not _WORKFLOW_TYPE_NAME.fullmatch(self.workflow_type):
                raise ControlContractError(
                    "workflow_type", "must be an exact registered Workflow type name"
                )
        elif not isinstance(self.workflow_type, type) or getattr(
            self.workflow_type, "__temporal_workflow_definition", None
        ) is None:
            raise ControlContractError(
                "workflow_type", "must be a type name or a decorated Workflow class"
            )
        if not callable(self.workflow_id_builder):
            raise ControlContractError("workflow_id_builder", "must be callable")
        if inspect.iscoroutinefunction(self.workflow_id_builder) or inspect.iscoroutinefunction(
            getattr(  # noqa: B004 - inspecting the already-proven callable's bound method
                self.workflow_id_builder, "__call__", None
            )
        ):
            raise ControlContractError(
                "workflow_id_builder", "must be synchronous; it is run through asyncio.to_thread"
            )
        if self.action == "start":
            if self.signal_name is not None:
                raise ControlContractError("signal_name", "a start carries no Signal name")
            if self.control_contract_type is not ControlCommand:
                raise ControlContractError(
                    "control_contract_type", "a start carries a ControlCommand"
                )
        elif self.action == "signal":
            if self.signal_name not in STANDARD_SIGNAL_NAMES:
                raise ControlContractError("signal_name", "not a standard Pacha Signal name")
            if self.control_contract_type is not ControlSignal:
                raise ControlContractError(
                    "control_contract_type", "a Signal carries a ControlSignal"
                )
        else:
            raise ControlContractError("action", "must be exactly 'start' or 'signal'")


def _chase_workflow_id(event: Event) -> WorkflowRef:
    """Resolve a chase event's checklist ULID without carrying its payload on."""

    payload = event.payload
    checklist_ref = payload.get("checklist_id") if isinstance(payload, dict) else None
    if not isinstance(checklist_ref, str):
        raise ControlContractError(
            "checklist_ref", "a chase orchestration event requires checklist_id"
        )
    return chase_workflow_ref(checklist_ref)


def _chase_mapping(
    event_type: str,
    *,
    action: Literal["start", "signal"],
    signal_name: str | None = None,
) -> TemporalIntentMapping:
    return TemporalIntentMapping(
        event_type=event_type,
        workflow_type="DocumentChaseWorkflow",
        workflow_id_builder=_chase_workflow_id,
        action=action,
        signal_name=signal_name,
        control_contract_type=ControlCommand if action == "start" else ControlSignal,
    )


#: T03's complete production routing surface. Every event carries checklist
#: detail only in PostgreSQL; the bridge forwards either the generic
#: `ControlCommand` or one opaque event ULID.
TEMPORAL_INTENT_MAPPINGS: tuple[TemporalIntentMapping, ...] = (
    _chase_mapping("chase.workflow_requested", action="start"),
    _chase_mapping("chase.item_requested", action="signal", signal_name="pacha_event"),
    _chase_mapping(
        "chase.item_received", action="signal", signal_name="document_received"
    ),
    _chase_mapping(
        "chase.item_verified", action="signal", signal_name="document_received"
    ),
    _chase_mapping("chase.item_rejected", action="signal", signal_name="pacha_event"),
    _chase_mapping("chase.item_waived", action="signal", signal_name="pacha_event"),
    _chase_mapping(
        "chase.item_snoozed", action="signal", signal_name="snooze_changed"
    ),
    _chase_mapping("chase.reminder_sent", action="signal", signal_name="pacha_event"),
    _chase_mapping("chase.complete", action="signal", signal_name="pacha_event"),
    _chase_mapping("chase.cancelled", action="signal", signal_name="claim_terminal"),
    _chase_mapping(
        "chase.inbound_received", action="signal", signal_name="inbound_received"
    ),
    _chase_mapping(
        "chase.review_resolved", action="signal", signal_name="review_resolved"
    ),
)


class TemporalIntentConsumer:
    """Outbox consumer translating committed events into Temporal intents."""

    def __init__(
        self,
        starter: TemporalStarter,
        mappings: Sequence[TemporalIntentMapping],
    ) -> None:
        by_type: dict[str, TemporalIntentMapping] = {}
        for mapping in mappings:
            if not isinstance(mapping, TemporalIntentMapping):
                raise ControlContractError("mappings", "must be TemporalIntentMapping instances")
            if mapping.event_type in by_type:
                raise ControlContractError(
                    "event_type", f"{mapping.event_type!r} is mapped more than once"
                )
            by_type[mapping.event_type] = mapping
        self._starter = starter
        self._mappings = by_type

    @property
    def event_types(self) -> frozenset[str]:
        """The exact set of event types this consumer acts on."""

        return frozenset(self._mappings)

    async def __call__(self, event: Event) -> None:
        """Start or Signal for one committed event, or acknowledge a no-op.

        Two cases return normally and are therefore marked succeeded: an event
        type with no mapping, and a mapped event whose builder resolves to
        `None`. Both are deterministic decisions that the event has no Temporal
        target, not failures — retrying either would never produce a different
        answer. A builder *exception*, by contrast, propagates, so the delivery
        retries and eventually dead-letters with the existing `ops.alert`.
        """

        mapping = self._mappings.get(event.type)
        if mapping is None:
            return

        # The builder is synchronous and may read PostgreSQL to resolve the
        # subject, so it must not run on the Activity's event loop.
        workflow_ref = await asyncio.to_thread(mapping.workflow_id_builder, event)
        if workflow_ref is None:
            return
        if not isinstance(workflow_ref, WorkflowRef):
            raise ControlContractError(
                "workflow_id_builder", "must return a WorkflowRef or None"
            )

        if mapping.action == "start":
            # The correlation id is the run ULID the initiating transaction
            # already committed. If it is not a ULID the mapping is wrong, and
            # minting one here would break `USE_EXISTING` de-duplication.
            validate_control_field("run_ref", event.correlation_id)
            await self._starter.start(
                workflow_type=mapping.workflow_type,
                workflow_ref=workflow_ref,
                command=ControlCommand(
                    run_ref=event.correlation_id,
                    claim_ref=event.claim_id,
                    trigger_event_ref=event.id,
                    event_ref=event.id,
                ),
            )
            return

        assert mapping.signal_name is not None  # noqa: S101 - guaranteed at construction
        await self._starter.signal(
            workflow_ref=workflow_ref,
            signal_name=mapping.signal_name,
            signal=ControlSignal(event_ref=event.id),
        )
