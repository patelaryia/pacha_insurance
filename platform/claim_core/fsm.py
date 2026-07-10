"""Claim lifecycle finite-state machine and its sole mutation point."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from claim_core.database import ClaimLocks, acquire_database_claim_lock
from claim_core.errors import ClaimCoreError
from claim_core.models import Claim, ClaimField


class ClaimState(StrEnum):
    """The exhaustive PRD-00 v1.1 primary claim state set."""

    INTIMATED = "INTIMATED"
    TRIAGED = "TRIAGED"
    AWAITING_DOCS = "AWAITING_DOCS"
    IN_ASSESSMENT = "IN_ASSESSMENT"
    REPORT_RECEIVED = "REPORT_RECEIVED"
    REGISTERED = "REGISTERED"
    RESERVED = "RESERVED"
    PACK_READY = "PACK_READY"
    IN_APPROVAL = "IN_APPROVAL"
    APPROVED = "APPROVED"
    IN_REPAIR = "IN_REPAIR"
    REINSPECTION = "REINSPECTION"
    RELEASED = "RELEASED"
    WRITE_OFF = "WRITE_OFF"
    SALVAGE_BIDDING = "SALVAGE_BIDDING"
    CLIENT_ELECTION = "CLIENT_ELECTION"
    SURRENDER_CHECKLIST = "SURRENDER_CHECKLIST"
    RETAINED = "RETAINED"
    SETTLEMENT = "SETTLEMENT"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"
    DECLINED = "DECLINED"
    WITHDRAWN = "WITHDRAWN"
    VOID = "VOID"


@dataclass(frozen=True)
class StateMetadata:
    """Lifecycle semantics consumed by later SLA and chase packets."""

    is_terminal: bool = False
    suppresses_activity: bool = False
    reopenable: bool = False


STATE_METADATA: dict[ClaimState, StateMetadata] = {
    state: StateMetadata() for state in ClaimState
}
STATE_METADATA.update(
    {
        ClaimState.DECLINED: StateMetadata(True, True, True),
        ClaimState.WITHDRAWN: StateMetadata(True, True, True),
        ClaimState.VOID: StateMetadata(True, True, False),
        ClaimState.SETTLED: StateMetadata(False, True, False),
        ClaimState.CLOSED: StateMetadata(True, True, False),
    }
)


PRIMARY_TRANSITIONS: dict[ClaimState, frozenset[ClaimState]] = {
    ClaimState.INTIMATED: frozenset({ClaimState.TRIAGED}),
    ClaimState.TRIAGED: frozenset({ClaimState.AWAITING_DOCS}),
    ClaimState.AWAITING_DOCS: frozenset({ClaimState.IN_ASSESSMENT}),
    ClaimState.IN_ASSESSMENT: frozenset({ClaimState.REPORT_RECEIVED}),
    ClaimState.REPORT_RECEIVED: frozenset(
        {ClaimState.WRITE_OFF, ClaimState.REGISTERED}
    ),
    ClaimState.REGISTERED: frozenset({ClaimState.RESERVED}),
    ClaimState.RESERVED: frozenset({ClaimState.PACK_READY}),
    ClaimState.PACK_READY: frozenset({ClaimState.IN_APPROVAL}),
    ClaimState.IN_APPROVAL: frozenset({ClaimState.APPROVED, ClaimState.PACK_READY}),
    ClaimState.APPROVED: frozenset({ClaimState.IN_REPAIR}),
    ClaimState.IN_REPAIR: frozenset({ClaimState.REINSPECTION}),
    ClaimState.REINSPECTION: frozenset({ClaimState.RELEASED}),
    ClaimState.RELEASED: frozenset({ClaimState.SETTLEMENT}),
    ClaimState.WRITE_OFF: frozenset({ClaimState.SALVAGE_BIDDING}),
    ClaimState.SALVAGE_BIDDING: frozenset({ClaimState.CLIENT_ELECTION}),
    ClaimState.CLIENT_ELECTION: frozenset(
        {ClaimState.SURRENDER_CHECKLIST, ClaimState.RETAINED}
    ),
    ClaimState.SURRENDER_CHECKLIST: frozenset({ClaimState.SETTLEMENT}),
    ClaimState.RETAINED: frozenset({ClaimState.SETTLEMENT}),
    ClaimState.SETTLEMENT: frozenset({ClaimState.SETTLED}),
    ClaimState.SETTLED: frozenset({ClaimState.CLOSED}),
    ClaimState.CLOSED: frozenset(),
    ClaimState.DECLINED: frozenset(),
    ClaimState.WITHDRAWN: frozenset(),
    ClaimState.VOID: frozenset(),
}

RULE_LINKED_GUARDS: dict[tuple[ClaimState, ClaimState], str] = {
    (ClaimState.INTIMATED, ClaimState.TRIAGED): "coverage+excess evaluated",
    (ClaimState.AWAITING_DOCS, ClaimState.IN_ASSESSMENT): "estimate received",
    (ClaimState.IN_ASSESSMENT, ClaimState.REPORT_RECEIVED): "assessor report parsed",
    (ClaimState.REPORT_RECEIVED, ClaimState.WRITE_OFF): "R-05 true",
    (
        ClaimState.REGISTERED,
        ClaimState.RESERVED,
    ): (
        "C-02/C-03 EXECUTED LOCALLY with verified inputs — projection is a parallel "
        "tracker, NOT a guard; see PRD-08 §8.2"
    ),
    (ClaimState.RESERVED, ClaimState.PACK_READY): "manifest complete + note drafted",
    (ClaimState.PACK_READY, ClaimState.IN_APPROVAL): "officer signed note",
    (ClaimState.IN_REPAIR, ClaimState.REINSPECTION): "R-08 routing",
    (
        ClaimState.SURRENDER_CHECKLIST,
        ClaimState.SETTLEMENT,
    ): "complete, R-13/R-14 gate",
}

PRE_REGISTERED_STATES = frozenset(
    {
        ClaimState.INTIMATED,
        ClaimState.TRIAGED,
        ClaimState.AWAITING_DOCS,
        ClaimState.IN_ASSESSMENT,
        ClaimState.REPORT_RECEIVED,
    }
)
DECLINE_APPROVAL_STATES = frozenset(
    {
        ClaimState.AWAITING_DOCS,
        ClaimState.IN_ASSESSMENT,
        ClaimState.REPORT_RECEIVED,
        ClaimState.REGISTERED,
        ClaimState.RESERVED,
        ClaimState.PACK_READY,
    }
)
DECLINE_REASONS = frozenset(
    {
        "below_excess",
        "out_of_cover",
        "fraud",
        "non_disclosure",
        "late_intimation",
        "other",
    }
)
WITHDRAWAL_BLOCKED_STATES = frozenset(
    {
        ClaimState.SETTLEMENT,
        ClaimState.SETTLED,
        ClaimState.CLOSED,
        ClaimState.DECLINED,
        ClaimState.WITHDRAWN,
        ClaimState.VOID,
    }
)

EventRecorder = Callable[..., Any]


@dataclass(frozen=True)
class TransitionResult:
    """A committed state summary or a staged decline-approval result."""

    claim_id: str
    status: ClaimState
    substatus: str | None
    approval_required: bool = False


class ClaimStateMachine:
    """Enforce and commit every post-creation claim status/substatus mutation."""

    def __init__(
        self,
        session_factory: sessionmaker,
        claim_locks: ClaimLocks,
        clock: Callable[[], datetime],
        event_recorder: EventRecorder,
    ) -> None:
        self._sessions = session_factory
        self._claim_locks = claim_locks
        self._clock = clock
        self._event = event_recorder

    @staticmethod
    def _claim_or_error(session: Session, claim_id: str) -> Claim:
        claim = session.get(Claim, claim_id)
        if claim is None:
            raise ClaimCoreError(404, "CLAIM_NOT_FOUND", f"Claim {claim_id} was not found")
        return claim

    @staticmethod
    def _parse_state(value: str) -> ClaimState:
        try:
            return ClaimState(value)
        except ValueError as error:
            raise ClaimCoreError(
                422, "UNKNOWN_STATE", f"Claim state {value!r} is not registered"
            ) from error

    @staticmethod
    def _legal_successors(current: ClaimState) -> frozenset[ClaimState]:
        successors = set(PRIMARY_TRANSITIONS[current])
        if current in PRE_REGISTERED_STATES:
            successors.add(ClaimState.VOID)
        if current not in WITHDRAWAL_BLOCKED_STATES:
            successors.add(ClaimState.WITHDRAWN)
        return frozenset(successors)

    @staticmethod
    def _illegal_transition(current: ClaimState, requested: ClaimState) -> ClaimCoreError:
        successors = sorted(state.value for state in ClaimStateMachine._legal_successors(current))
        legal = ", ".join(successors) if successors else "none"
        return ClaimCoreError(
            409,
            "ILLEGAL_TRANSITION",
            f"Cannot transition claim from {current.value} to {requested.value}; "
            f"legal successors: {legal}",
        )

    @staticmethod
    def _has_icon_claim_number(session: Session, claim_id: str) -> bool:
        field_id = session.scalar(
            select(ClaimField.id)
            .where(
                ClaimField.claim_id == claim_id,
                ClaimField.path == "external.icon.claim_no",
                ClaimField.superseded_by.is_(None),
            )
            .limit(1)
        )
        return field_id is not None

    @staticmethod
    def _structured_reject_reasons(payload: dict[str, Any]) -> list[dict[str, str]]:
        reasons = payload.get("reasons")
        valid = (
            isinstance(reasons, list)
            and bool(reasons)
            and all(
                isinstance(reason, dict)
                and set(reason) == {"code", "detail"}
                and isinstance(reason["code"], str)
                and bool(reason["code"].strip())
                and isinstance(reason["detail"], str)
                and bool(reason["detail"].strip())
                for reason in reasons
            )
        )
        if not valid:
            raise ClaimCoreError(
                422,
                "REJECT_REASONS_REQUIRED",
                "Rejecting an approval requires non-empty structured reasons",
            )
        return reasons

    def _commit_status(
        self,
        session: Session,
        claim: Claim,
        current: ClaimState,
        requested: ClaimState,
        *,
        actor: str,
        correlation_id: str,
        payload: dict[str, Any],
        decline_reason: str | None = None,
    ) -> None:
        if decline_reason is None and requested not in self._legal_successors(current):
            raise self._illegal_transition(current, requested)

        event_payload: dict[str, Any] = {"from": current.value, "to": requested.value}
        if decline_reason is not None:
            event_payload["reason"] = decline_reason

        if current == ClaimState.IN_APPROVAL and requested == ClaimState.PACK_READY:
            event_payload["reason"] = self._structured_reject_reasons(payload)

        if (
            current == ClaimState.REPORT_RECEIVED
            and requested == ClaimState.REGISTERED
            and not self._has_icon_claim_number(session, claim.id)
        ):
            blocked_on = ["external.icon.claim_no not captured"]
            raise ClaimCoreError(
                409,
                "TRANSITION_GUARD_BLOCKED",
                "Claim transition is blocked by structural guards",
                extra={"blocked_on": blocked_on},
            )

        guard = RULE_LINKED_GUARDS.get((current, requested))
        if guard is not None:
            event_payload["guards_pending"] = [guard]

        claim.status = requested.value
        claim.substatus = None
        claim.updated_at = self._clock()
        if requested == ClaimState.CLOSED:
            claim.closed_at = self._clock()
        self._event(
            session,
            claim_id=claim.id,
            event_type="claim.status_changed",
            payload=event_payload,
            actor=actor,
            correlation_id=correlation_id,
        )

    def transition(
        self,
        claim_id: str,
        *,
        actor: str,
        correlation_id: str,
        to: str | None = None,
        payload: dict[str, Any] | None = None,
        decline_reason: str | None = None,
        substatus: str | None | object = ...,
    ) -> TransitionResult:
        """Validate and atomically commit one state action under the claim lock."""

        requested = self._parse_state(to) if to is not None else None
        request_payload = payload or {}
        with self._claim_locks.acquire(claim_id):
            with self._sessions.begin() as session:
                acquire_database_claim_lock(session, claim_id)
                claim = self._claim_or_error(session, claim_id)
                current = self._parse_state(claim.status)

                if substatus is not ...:
                    if requested is not None or decline_reason is not None:
                        raise RuntimeError("substatus actions cannot also change primary state")
                    self._transition_substatus(
                        session,
                        claim,
                        current,
                        substatus,
                        actor=actor,
                        correlation_id=correlation_id,
                    )
                elif decline_reason is not None:
                    if requested != ClaimState.DECLINED:
                        raise RuntimeError("decline actions must request DECLINED")
                    if decline_reason not in DECLINE_REASONS:
                        raise ClaimCoreError(
                            422,
                            "INVALID_DECLINE_REASON",
                            f"Decline reason {decline_reason!r} is not registered",
                        )
                    if current == ClaimState.TRIAGED:
                        self._commit_status(
                            session,
                            claim,
                            current,
                            requested,
                            actor=actor,
                            correlation_id=correlation_id,
                            payload=request_payload,
                            decline_reason=decline_reason,
                        )
                    elif current in DECLINE_APPROVAL_STATES:
                        self._event(
                            session,
                            claim_id=claim.id,
                            event_type="review.created",
                            payload={
                                "type": "EXCEPTION",
                                "subtype": "decline_approval_required",
                                "reason": decline_reason,
                                "requested_by": actor,
                            },
                            actor=actor,
                            correlation_id=correlation_id,
                        )
                        claim.updated_at = self._clock()
                        return TransitionResult(
                            claim.id, current, claim.substatus, approval_required=True
                        )
                    else:
                        raise self._illegal_transition(current, requested)
                else:
                    if requested is None:
                        raise RuntimeError("a primary transition requires a target state")
                    self._commit_status(
                        session,
                        claim,
                        current,
                        requested,
                        actor=actor,
                        correlation_id=correlation_id,
                        payload=request_payload,
                    )

                return TransitionResult(
                    claim.id, self._parse_state(claim.status), claim.substatus
                )

    def decline(
        self, claim_id: str, *, reason: str, actor: str, correlation_id: str
    ) -> TransitionResult:
        """Run the reasoned decline action through the central transition method."""

        return self.transition(
            claim_id,
            actor=actor,
            correlation_id=correlation_id,
            to=ClaimState.DECLINED.value,
            decline_reason=reason,
        )

    def set_substatus(
        self,
        claim_id: str,
        *,
        substatus: str | None,
        actor: str,
        correlation_id: str,
    ) -> TransitionResult:
        """Run the closed-set substatus action through the central transition method."""

        return self.transition(
            claim_id,
            actor=actor,
            correlation_id=correlation_id,
            substatus=substatus,
        )

    def _transition_substatus(
        self,
        session: Session,
        claim: Claim,
        current: ClaimState,
        substatus: str | None | object,
        *,
        actor: str,
        correlation_id: str,
    ) -> None:
        if substatus not in {None, "EX_GRATIA_REVIEW"}:
            raise ClaimCoreError(
                422,
                "UNKNOWN_SUBSTATUS",
                f"Claim substatus {substatus!r} is not registered",
            )
        if current != ClaimState.DECLINED:
            raise ClaimCoreError(
                409,
                "SUBSTATUS_NOT_ALLOWED",
                "EX_GRATIA_REVIEW substatus is permitted only while DECLINED",
            )
        claim.substatus = substatus
        claim.updated_at = self._clock()
        if substatus == "EX_GRATIA_REVIEW":
            self._event(
                session,
                claim_id=claim.id,
                event_type="review.created",
                payload={"type": "EXCEPTION", "subtype": "ex_gratia_review"},
                actor=actor,
                correlation_id=correlation_id,
            )
