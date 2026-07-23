"""AR-2 single choke point for governed side effects.

PACKET-21 (register #278) adds one curated extension: a *deferred* executor
contract. A deferred executor authorises a side effect and returns a durably
leaseable handle rather than a completed write, so authorisation and the
eventual reconciled outcome share one durable `agent_run`. Nothing else about
the gate changes, and every existing synchronous executor behaves exactly as
before.

This module is also the only lawful call site of `Adapter.execute`: see
:func:`execute_authorised_adapter`, which refuses anything but a currently
leased, authenticated work receipt issued by the projection control API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from claim_core import new_ulid
from cop_runtime.contracts import REVIEW_ITEM_TYPES
from eval_harness.gating import grade_output

DEFERRED_STATUSES = frozenset({"completed", "failed", "blocked"})


@dataclass(frozen=True)
class Action:
    """A typed, gradeable request passed through the autonomy gate."""

    type: str
    payload: dict[str, Any]
    grader_id: str | None = None
    log_payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("action type must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise TypeError("action payload must be a mapping")
        if self.log_payload is not None and not isinstance(self.log_payload, dict):
            raise TypeError("action log payload must be a mapping")


class ExecutionRefused(RuntimeError):
    """An executor's external dependency refused before any write occurred."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DeferredAction:
    """An authorised, durably leaseable side effect that has *not* happened yet.

    The gate leaves the owning `agent_run` running; only the owning coordinator
    may end it, through :meth:`AutonomyGate.finish_deferred`, once it holds a
    reconciled terminal outcome.
    """

    run_id: str
    ref: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("a deferred action requires the owning run id")
        if not isinstance(self.ref, dict):
            raise TypeError("a deferred action ref must be a mapping")


@dataclass(frozen=True)
class WorkReceipt:
    """Proof that one adapter execution is currently leased and authorised.

    Issued only by the projection control API when it grants a lease, and
    consumed only by :func:`execute_authorised_adapter`. It carries ids and
    times — never a target value, credential, selector, or raw lease token.
    """

    run_id: str
    projection_id: str
    operation: str
    definition_version: str
    attempt: int
    expires_at: datetime
    lease_token_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "projection_id",
            "operation",
            "definition_version",
            "lease_token_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"work receipt {name} is required")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise TypeError("work receipt attempt must be an integer")
        if self.attempt < 1:
            raise ValueError("work receipt attempt must be at least one")
        if not isinstance(self.expires_at, datetime):
            raise TypeError("work receipt expires_at must be a datetime")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def execute_authorised_adapter(
    adapter: Any,
    *,
    receipt: WorkReceipt,
    op: Any,
    payload: dict[str, Any],
    run_id: str,
    now: datetime,
) -> Any:
    """The one lawful `Adapter.execute` call site (PACKET-21 §3).

    Refuses anything that is not a live, matching, authenticated work receipt.
    A refusal here has caused no external write, so it is safe to raise.
    """

    if not isinstance(receipt, WorkReceipt):
        raise ExecutionRefused("adapter_receipt_missing")
    if receipt.run_id != run_id:
        raise ExecutionRefused("adapter_receipt_run_mismatch")
    if _aware(receipt.expires_at) <= _aware(now):
        raise ExecutionRefused("adapter_lease_expired")
    system = getattr(adapter, "system", None)
    if not isinstance(system, str) or not receipt.operation.startswith(f"{system}."):
        raise ExecutionRefused("adapter_system_mismatch")
    return adapter.execute(op, payload, run_id)


def _is_funds_transfer(action_type: str) -> bool:
    normalised = action_type.lower().replace("-", "_")
    if any(
        marker in normalised
        for marker in ("eft_transfer", "funds_transfer", "transfer_funds")
    ):
        return True
    if normalised.startswith("settlement.") and normalised.rsplit(".", 1)[-1] in {
        "pay",
        "payment",
        "disburse",
        "transfer",
    }:
        return True
    return "payment_voucher" in normalised and normalised.endswith(
        (".execute", ".release", ".send")
    )


def _valid_confirm_entry(entry: Any) -> bool:
    """A confirm type is a closed review type, optionally with a subtype."""

    if isinstance(entry, str):
        return entry in REVIEW_ITEM_TYPES
    return (
        isinstance(entry, dict)
        and set(entry) == {"type", "subtype"}
        and entry["type"] in REVIEW_ITEM_TYPES
        and isinstance(entry["subtype"], str)
        and bool(entry["subtype"])
    )


def _confirm_entry(entry: Any) -> tuple[str, str | None]:
    if isinstance(entry, dict):
        return str(entry["type"]), str(entry["subtype"])
    return str(entry), None


def load_gate_config(path: Path) -> dict[str, Any]:
    """Validate closed review types and transport/exemption pack data."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid agent gate config: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("agent gate config requires version 1")
    confirm_types = payload.get("confirm_types")
    exempt = payload.get("exempt_capabilities")
    transport = payload.get("transport")
    if (
        not isinstance(confirm_types, dict)
        or not all(
            isinstance(capability, str) and _valid_confirm_entry(item_type)
            for capability, item_type in confirm_types.items()
        )
        or not isinstance(exempt, list)
        or not all(isinstance(value, str) and value for value in exempt)
        or not isinstance(transport, dict)
        or transport.get("status") != "pending_capture"
    ):
        raise ValueError("agent gate config contains invalid values")
    return {
        "confirm_types": dict(confirm_types),
        "exempt_capabilities": tuple(exempt),
        "transport": dict(transport),
    }


class AutonomyGate:
    """Resolve capability level, grading, staging, execution, and sampling."""

    def __init__(
        self,
        app: Any,
        runner: Any,
        config: dict[str, Any],
        *,
        grade: Callable[[Action, str, str | None], Any] | None,
    ) -> None:
        self.app = app
        self.runner = runner
        self.config = config
        self.grade = grade
        self._executors: dict[str, Callable[[Action], Any]] = {}
        self._deferred: dict[str, Callable[[Action, str], DeferredAction]] = {}

    def register_executor(self, action_type: str, fn: Callable[[Action], Any]) -> None:
        """Register a typed executor while enforcing the no-payment constitution."""

        if _is_funds_transfer(action_type):
            raise ValueError(f"funds-transfer action {action_type!r} is forbidden")
        if not action_type or action_type in self._executors or not callable(fn):
            raise ValueError(f"executor {action_type!r} is already registered or invalid")
        self._executors[action_type] = fn

    def register_deferred_executor(
        self, action_type: str, fn: Callable[[Action, str], DeferredAction]
    ) -> None:
        """Register a typed executor that authorises rather than completes a write."""

        if _is_funds_transfer(action_type):
            raise ValueError(f"funds-transfer action {action_type!r} is forbidden")
        if (
            not action_type
            or action_type in self._deferred
            or action_type in self._executors
            or not callable(fn)
        ):
            raise ValueError(
                f"deferred executor {action_type!r} is already registered or invalid"
            )
        self._deferred[action_type] = fn

    def execute_staged(self, action: Action, *, run_id: str | None = None) -> Any:
        """Execute an action that has already passed the gate and human approval."""

        if _is_funds_transfer(action.type):
            raise ValueError(f"funds-transfer action {action.type!r} is forbidden")
        deferred = self._deferred.get(action.type)
        if deferred is not None:
            if not isinstance(run_id, str) or not run_id:
                raise ExecutionRefused("deferred_run_id_required")
            return deferred(action, run_id)
        executor = self._executors.get(action.type)
        if executor is None:
            raise ExecutionRefused("executor_not_registered")
        return executor(action)

    def finish_deferred(
        self,
        run_id: str,
        *,
        status: str,
        outcome: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        """End one deferred run once its coordinator holds a reconciled outcome.

        ``outcome['stages']`` records the coarse COP stages the coordinator
        actually reached, so the durable AR-1 step list stays truthful without a
        second progress channel.
        """

        if status not in DEFERRED_STATUSES:
            raise ValueError(f"deferred status {status!r} is not terminal")
        if not isinstance(outcome, dict):
            raise TypeError("deferred outcome must be a mapping")
        self.runner.finish_deferred_run(
            run_id, status=status, outcome=outcome, error=error
        )

    def _capability(self, capability_id: str) -> tuple[str, dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text("SELECT current_level, policy FROM capabilities WHERE id = :id"),
                {"id": capability_id},
            ).first()
        if row is None or row[0] not in {"L0", "L1", "L2", "L3", "L4"}:
            raise ValueError(f"unknown capability {capability_id!r}")
        raw_policy = row[1]
        if isinstance(raw_policy, str):
            raw_policy = json.loads(raw_policy)
        return str(row[0]), dict(raw_policy or {})

    def _grade_blocked(
        self,
        action: Action,
        capability_id: str,
        claim_id: str | None,
        actor: str,
    ) -> bool:
        if action.grader_id is None:
            return False
        if self.grade is not None:
            return bool(self.grade(action, capability_id, claim_id).blocked)
        subject = {
            **action.payload,
            "action_type": action.type,
            "capability_id": capability_id,
            "claim_id": claim_id,
        }
        return bool(
            grade_output(
                self.app.state.eval_harness,
                action.grader_id,
                subject,
                actor=actor,
            ).blocked
        )

    def _emit_review(
        self,
        *,
        run_id: str,
        review_type: str,
        capability_id: str,
        action: Action,
        claim_id: str | None,
        actor: str,
        subtype: str | None = None,
    ) -> str:
        review_id = new_ulid()
        staged_action: dict[str, Any] = {
            "type": action.type,
            "payload": dict(action.payload),
        }
        recipients = action.payload.get("to_party_ids")
        if action.type == "communication.send" and isinstance(recipients, list):
            staged_action["to_party_ids"] = list(recipients)
        payload: dict[str, Any] = {
            "review_id": review_id,
            "type": review_type,
            "agent_run_id": run_id,
            "capability_id": capability_id,
            "action": staged_action,
        }
        if subtype is not None:
            payload["subtype"] = subtype
        retry_of = action.payload.get("retry_of")
        if isinstance(retry_of, str):
            payload["retry_of"] = retry_of
        with self.runner.sessions.begin() as session:
            self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload=payload,
                actor=actor,
                correlation_id=run_id,
            )
        return review_id

    @staticmethod
    def _agent_name(actor: str) -> str:
        return actor.removeprefix("agent:") if actor.startswith("agent:") else "runtime"

    def execute_or_stage(
        self,
        *,
        capability_id: str,
        action: Action,
        claim_id: str | None,
        actor: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply the exact L0–L4 semantics and return a stable outcome mapping."""

        if _is_funds_transfer(action.type):
            raise ValueError(f"funds-transfer action {action.type!r} is forbidden")
        level, _policy = self._capability(capability_id)
        owns_run = run_id is None
        if run_id is None:
            run_id = self.runner.record_action_start(
                agent=self._agent_name(actor),
                capability_id=capability_id,
                claim_id=claim_id,
                action_type=action.type,
                autonomy_level=level,
            )
        blocked = self._grade_blocked(action, capability_id, claim_id, actor)
        effective_level = "L1" if blocked else level

        if effective_level == "L0":
            with self.runner.sessions.begin() as session:
                self.app.state.record_event(
                    session,
                    claim_id=claim_id,
                    event_type="agent.action_logged",
                    payload={
                        "agent_run_id": run_id,
                        "capability_id": capability_id,
                        "action_type": action.type,
                    },
                    actor=actor,
                    correlation_id=run_id,
                )
            outcome = {
                "status": "logged",
                "review_id": None,
                "review_type": None,
                "sampled": False,
            }
            if action.log_payload is not None:
                outcome["log_payload"] = dict(action.log_payload)
            if owns_run:
                self.runner.finish_action(run_id, status="completed", outcome=outcome)
            return outcome

        if effective_level in {"L1", "L2"}:
            review_type = "DRAFT_RELEASE"
            subtype: str | None = None
            if effective_level == "L2":
                review_type, subtype = _confirm_entry(
                    self.config["confirm_types"].get(capability_id, "DRAFT_RELEASE")
                )
            review_id = self._emit_review(
                run_id=run_id,
                review_type=review_type,
                capability_id=capability_id,
                action=action,
                claim_id=claim_id,
                actor=actor,
                subtype="grader_blocked" if blocked else subtype,
            )
            outcome = {
                "status": "staged",
                "review_id": review_id,
                "review_type": review_type,
                "sampled": False,
            }
            if owns_run:
                self.runner.finish_action(run_id, status="awaiting_review", outcome=outcome)
            return outcome

        deferred = self._deferred.get(action.type)
        if deferred is not None:
            deferred(action, run_id)
            sampled = False
            if level == "L3":
                sampled = self.app.state.eval_harness.autonomy.emit_sample_review(
                    capability_id,
                    run_id,
                    claim_id=claim_id,
                    underlying_type=action.type,
                    actor=actor,
                )
            # The run stays open: only the coordinator's reconciled terminal
            # outcome may end it (register #278).
            return {
                "status": "deferred",
                "review_id": None,
                "review_type": None,
                "sampled": sampled,
                "run_id": run_id,
            }

        executor = self._executors.get(action.type)
        if executor is None:
            error = {"code": "EXECUTOR_NOT_REGISTERED", "action_type": action.type}
            review_id = self._emit_review(
                run_id=run_id,
                review_type="EXCEPTION",
                subtype="executor_not_registered",
                capability_id=capability_id,
                action=action,
                claim_id=claim_id,
                actor=actor,
            )
            outcome = {
                "status": "refused",
                "review_id": review_id,
                "review_type": "EXCEPTION",
                "sampled": False,
            }
            if owns_run:
                self.runner.finish_action(run_id, status="blocked", outcome=outcome, error=error)
            return outcome
        try:
            executor(action)
        except ExecutionRefused as error:
            outcome = {
                "status": "refused",
                "review_id": None,
                "review_type": None,
                "sampled": False,
                "reason": error.reason,
            }
            if owns_run:
                self.runner.finish_action(
                    run_id,
                    status="blocked",
                    outcome=outcome,
                    error={"code": "EXECUTION_REFUSED", "reason": error.reason},
                )
            return outcome

        sampled = False
        if level == "L3":
            sampled = self.app.state.eval_harness.autonomy.emit_sample_review(
                capability_id,
                run_id,
                claim_id=claim_id,
                underlying_type=action.type,
                actor=actor,
            )
        outcome = {
            "status": "executed",
            "review_id": None,
            "review_type": None,
            "sampled": sampled,
        }
        if owns_run:
            self.runner.finish_action(run_id, status="completed", outcome=outcome)
        return outcome


__all__ = [
    "Action",
    "AutonomyGate",
    "DeferredAction",
    "ExecutionRefused",
    "WorkReceipt",
    "execute_authorised_adapter",
    "load_gate_config",
]
