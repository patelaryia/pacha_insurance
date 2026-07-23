"""The binding PRD-09 adapter, health, and result contracts (PACKET-21 §3).

Contracts only. Nothing here opens a browser, resolves a credential, or reaches
a target system: the concrete adapters live under ``projection_agent.runner``
and are constructed from the deployment-owned target registry.

``Adapter.execute`` has exactly one lawful call site — the curated AR-2 helper
``agent_runtime.gate.execute_authorised_adapter`` — which accepts only a
currently leased, authenticated work receipt returned by the control API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

#: PRD-09 retains a future `finance` system value. The v1 catalogue registers no
#: finance operation and installs no finance executor.
SYSTEMS: tuple[str, ...] = ("icon", "edms", "finance")
HEALTH_STATUSES: tuple[str, ...] = (
    "healthy",
    "degraded",
    "unavailable",
    "circuit_open",
)
#: The closed execution outcomes. An unrecognised target response is
#: `uncertain_write`, never a guessed known failure.
OUTCOMES: tuple[str, ...] = (
    "submitted",
    "completed_existing",
    "ui_drift",
    "known_failure",
    "uncertain_write",
)
#: Outcomes after which a write may have reached the target system.
POSSIBLE_WRITE_OUTCOMES: frozenset[str] = frozenset(
    {"submitted", "completed_existing", "uncertain_write"}
)

HEALTH_KEYS = frozenset({"status", "checked_at", "system", "runner_id", "reason_code"})
RESULT_KEYS = frozenset(
    {
        "outcome",
        "last_completed_step",
        "write_ids",
        "readback_keys",
        "evidence_ids",
        "reason_code",
    }
)


class AdapterContractError(ValueError):
    """A health or result payload is not the closed PRD-09 shape."""


def _closed(value: Any, allowed: tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AdapterContractError(f"{label} {value!r} is not a declared value")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"{label} must be a non-empty string or null")
    return value


def _id_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, tuple):
        items = list(value)
    elif isinstance(value, list):
        items = value
    else:
        raise AdapterContractError(f"{label} must be a sequence of ids")
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise AdapterContractError(f"{label} must contain non-empty ids")
    if len(set(items)) != len(items):
        raise AdapterContractError(f"{label} must not repeat an id")
    return tuple(items)


@dataclass(frozen=True)
class AdapterHealth:
    """One point-in-time adapter health reading. Closed status vocabulary."""

    status: Literal["healthy", "degraded", "unavailable", "circuit_open"]
    checked_at: datetime
    system: Literal["icon", "edms", "finance"]
    runner_id: str | None
    reason_code: str | None

    def __post_init__(self) -> None:
        _closed(self.status, HEALTH_STATUSES, "adapter health status")
        _closed(self.system, SYSTEMS, "adapter system")
        if not isinstance(self.checked_at, datetime):
            raise AdapterContractError("adapter health checked_at must be a datetime")
        _optional_text(self.runner_id, "adapter health runner_id")
        _optional_text(self.reason_code, "adapter health reason_code")

    @classmethod
    def from_mapping(cls, raw: Any) -> AdapterHealth:
        """Parse an untrusted mapping, rejecting unknown or missing keys."""

        if not isinstance(raw, dict) or set(raw) != HEALTH_KEYS:
            raise AdapterContractError("adapter health keys are invalid")
        checked_at = raw["checked_at"]
        if isinstance(checked_at, str):
            checked_at = datetime.fromisoformat(checked_at)
        return cls(
            status=raw["status"],
            checked_at=checked_at,
            system=raw["system"],
            runner_id=_optional_text(raw["runner_id"], "adapter health runner_id"),
            reason_code=_optional_text(raw["reason_code"], "adapter health reason_code"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at.isoformat(),
            "system": self.system,
            "runner_id": self.runner_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class OpResult:
    """The closed result of one adapter execution.

    Free-text target errors, stack traces, HTML, screenshots, credentials,
    selector contents, and target values are never carried here. The runner maps
    only captured failure signatures onto closed reason codes.
    """

    outcome: Literal[
        "submitted",
        "completed_existing",
        "ui_drift",
        "known_failure",
        "uncertain_write",
    ]
    last_completed_step: str | None
    write_ids: tuple[str, ...]
    readback_keys: dict[str, object]
    evidence_ids: tuple[str, ...]
    reason_code: str | None

    def __post_init__(self) -> None:
        _closed(self.outcome, OUTCOMES, "adapter outcome")
        _optional_text(self.last_completed_step, "last_completed_step")
        object.__setattr__(self, "write_ids", _id_tuple(self.write_ids, "write_ids"))
        object.__setattr__(
            self, "evidence_ids", _id_tuple(self.evidence_ids, "evidence_ids")
        )
        if not isinstance(self.readback_keys, dict) or not all(
            isinstance(key, str) and key for key in self.readback_keys
        ):
            raise AdapterContractError("readback_keys must be a mapping of named keys")
        _optional_text(self.reason_code, "reason_code")

    @property
    def may_have_written(self) -> bool:
        """Whether a target write may have reached the system (PRD-09 §9.5)."""

        return self.outcome in POSSIBLE_WRITE_OUTCOMES or bool(self.write_ids)

    @classmethod
    def from_mapping(cls, raw: Any) -> OpResult:
        """Parse an untrusted runner callback body, rejecting unknown keys."""

        if not isinstance(raw, dict) or set(raw) != RESULT_KEYS:
            raise AdapterContractError("adapter result keys are invalid")
        return cls(
            outcome=raw["outcome"],
            last_completed_step=_optional_text(
                raw["last_completed_step"], "last_completed_step"
            ),
            write_ids=_id_tuple(raw["write_ids"], "write_ids"),
            readback_keys=dict(raw["readback_keys"] or {}),
            evidence_ids=_id_tuple(raw["evidence_ids"], "evidence_ids"),
            reason_code=_optional_text(raw["reason_code"], "reason_code"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "last_completed_step": self.last_completed_step,
            "write_ids": list(self.write_ids),
            "readback_keys": dict(self.readback_keys),
            "evidence_ids": list(self.evidence_ids),
            "reason_code": self.reason_code,
        }


@runtime_checkable
class Adapter(Protocol):
    """The PRD-09 §9.4 target-system adapter contract."""

    system: Literal["icon", "edms", "finance"]

    def health(self) -> AdapterHealth: ...

    def execute(
        self,
        op: Any,
        payload: dict[str, object],
        run_id: str,
    ) -> OpResult: ...

    def readback(
        self,
        op: Any,
        keys: dict[str, object],
    ) -> dict[str, object]: ...


class AdapterUnavailable(RuntimeError):
    """A target record is blocked, so no adapter may be constructed."""

    def __init__(self, system: str, reason_code: str) -> None:
        self.system = system
        self.reason_code = reason_code
        super().__init__(f"{system} adapter unavailable: {reason_code}")


__all__ = [
    "Adapter",
    "AdapterContractError",
    "AdapterHealth",
    "AdapterUnavailable",
    "HEALTH_STATUSES",
    "OUTCOMES",
    "OpResult",
    "POSSIBLE_WRITE_OUTCOMES",
    "SYSTEMS",
]
