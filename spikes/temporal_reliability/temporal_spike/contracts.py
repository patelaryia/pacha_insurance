"""Control-only contracts allowed in Temporal history."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any

ALLOWED_FIELD_NAMES = frozenset(
    {
        "run_ref",
        "claim_ref",
        "trigger_event_ref",
        "review_event_ref",
        "payload_ref",
        "payload_hash",
        "write_id",
        "step",
        "status",
        "timer_seconds",
        "checkpoint_count",
    }
)
FORBIDDEN_FIELD_MARKERS = (
    "name",
    "email",
    "phone",
    "address",
    "bank",
    "account",
    "document",
    "narrative",
    "policy",
    "amount",
    "payee",
)


@dataclass(frozen=True)
class WorkflowInput:
    run_ref: str
    claim_ref: str
    trigger_event_ref: str
    payload_hash: str
    timer_seconds: int = 0


@dataclass(frozen=True)
class RunRef:
    run_ref: str
    claim_ref: str


@dataclass(frozen=True)
class StepCommand:
    run_ref: str
    claim_ref: str
    step: str


@dataclass(frozen=True)
class HeartbeatCommand:
    run_ref: str
    claim_ref: str
    checkpoint_count: int


@dataclass(frozen=True)
class ReviewCommand:
    run_ref: str
    review_event_ref: str


@dataclass(frozen=True)
class ExternalActionCommand:
    run_ref: str
    claim_ref: str
    payload_ref: str
    payload_hash: str
    write_id: str


@dataclass(frozen=True)
class ControlResult:
    status: str
    step: str
    payload_hash: str


def assert_control_only(value: Any) -> None:
    """Reject a Temporal contract that can carry a claim fact by construction."""

    if not is_dataclass(value):
        raise TypeError("Temporal contracts must be dataclasses")
    for field in fields(value):
        lowered = field.name.lower()
        if field.name not in ALLOWED_FIELD_NAMES or any(
            marker in lowered for marker in FORBIDDEN_FIELD_MARKERS
        ):
            raise ValueError(f"forbidden Temporal history field: {field.name}")
        item = getattr(value, field.name)
        if not isinstance(item, (str, int)):
            raise TypeError(f"unsupported Temporal history value: {field.name}")
        if isinstance(item, str) and len(item) > 160:
            raise ValueError(f"oversized Temporal control value: {field.name}")
