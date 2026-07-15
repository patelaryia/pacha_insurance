"""AR-2 single choke point for governed side effects."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from claim_core import new_ulid
from cop_runtime.contracts import REVIEW_ITEM_TYPES
from eval_harness.gating import grade_output


@dataclass(frozen=True)
class Action:
    """A typed, gradeable request passed through the autonomy gate."""

    type: str
    payload: dict[str, Any]
    grader_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("action type must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise TypeError("action payload must be a mapping")


class ExecutionRefused(RuntimeError):
    """An executor's external dependency refused before any write occurred."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


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
            isinstance(capability, str)
            and isinstance(item_type, str)
            and item_type in REVIEW_ITEM_TYPES
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

    def register_executor(self, action_type: str, fn: Callable[[Action], Any]) -> None:
        """Register a typed executor while enforcing the no-payment constitution."""

        if _is_funds_transfer(action_type):
            raise ValueError(f"funds-transfer action {action_type!r} is forbidden")
        if not action_type or action_type in self._executors or not callable(fn):
            raise ValueError(f"executor {action_type!r} is already registered or invalid")
        self._executors[action_type] = fn

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
        payload: dict[str, Any] = {
            "review_id": review_id,
            "type": review_type,
            "agent_run_id": run_id,
            "capability_id": capability_id,
            "action": {"type": action.type, "payload": dict(action.payload)},
        }
        if subtype is not None:
            payload["subtype"] = subtype
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
    ) -> dict[str, Any]:
        """Apply the exact L0–L4 semantics and return a stable outcome mapping."""

        if _is_funds_transfer(action.type):
            raise ValueError(f"funds-transfer action {action.type!r} is forbidden")
        level, _policy = self._capability(capability_id)
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
            self.runner.finish_action(run_id, status="completed", outcome=outcome)
            return outcome

        if effective_level in {"L1", "L2"}:
            review_type = "DRAFT_RELEASE"
            if effective_level == "L2":
                review_type = self.config["confirm_types"].get(
                    capability_id, "DRAFT_RELEASE"
                )
            review_id = self._emit_review(
                run_id=run_id,
                review_type=review_type,
                capability_id=capability_id,
                action=action,
                claim_id=claim_id,
                actor=actor,
                subtype="grader_blocked" if blocked else None,
            )
            outcome = {
                "status": "staged",
                "review_id": review_id,
                "review_type": review_type,
                "sampled": False,
            }
            self.runner.finish_action(run_id, status="awaiting_review", outcome=outcome)
            return outcome

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
        self.runner.finish_action(run_id, status="completed", outcome=outcome)
        return outcome


__all__ = [
    "Action",
    "AutonomyGate",
    "ExecutionRefused",
    "load_gate_config",
]
