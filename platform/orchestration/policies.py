"""The error and retry constitution (master plan section 12).

Retry values are pack data, per the config-over-code convention, but the
ceilings are not negotiable: they are hard-coded here and pack data may only
tighten them. Every numeric bound in the table is an upper bound, so a pack that
raises an interval, a backoff coefficient, an attempt count or a start-to-close
timeout above the tabulated value is rejected at load time rather than silently
granting an Activity more retries than the constitution allows.

Two policies are capped at a single Temporal attempt and cannot be relaxed at
all:

* `governed_external_write` — a timeout or Worker loss after the Activity is
  scheduled is uncertain, and PRD-09 forbids blind retry of an uncertain write;
* `provider_managed_retry` — the ED-4a provider wrapper already owns its own
  bounded retries, and Temporal must not multiply them. Its start-to-close
  ceiling is ED-4a's own ten-minute total, which Section 0 makes binding.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from temporalio.common import RetryPolicy

from orchestration.errors import RetryPolicyError

__all__ = [
    "POLICY_CEILINGS",
    "ActivityPolicy",
    "PolicyCeiling",
    "load_retry_policies",
    "parse_duration",
]

_DURATION_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$")
_DURATION_UNITS: Mapping[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

_TOP_LEVEL_KEYS = frozenset({"version", "retry_policies"})
_SUPPORTED_VERSION = 1


def parse_duration(name: str, value: Any) -> timedelta:
    """Parse a strict `{integer}{ms|s|m|h}` duration, refusing anything else."""

    if not isinstance(value, str) or not (match := _DURATION_PATTERN.fullmatch(value)):
        raise RetryPolicyError(f"{name} must be a duration such as '30s', '2m' or '2h'")
    magnitude = int(match.group(1))
    if magnitude <= 0:
        raise RetryPolicyError(f"{name} must be a positive duration")
    return timedelta(seconds=magnitude * _DURATION_UNITS[match.group(2)])


@dataclass(frozen=True, slots=True)
class PolicyCeiling:
    """The hard upper bound for one named policy. Pack data may only tighten."""

    start_to_close_timeout: timedelta
    maximum_attempts: int
    initial_interval: timedelta | None = None
    backoff_coefficient: float | None = None
    maximum_interval: timedelta | None = None
    heartbeat_timeout: timedelta | None = None
    single_attempt: bool = False

    @property
    def permitted_keys(self) -> frozenset[str]:
        keys = {"start_to_close_timeout", "maximum_attempts"}
        if self.initial_interval is not None:
            keys.add("initial_interval")
        if self.backoff_coefficient is not None:
            keys.add("backoff_coefficient")
        if self.maximum_interval is not None:
            keys.add("maximum_interval")
        if self.heartbeat_timeout is not None:
            keys.add("heartbeat_timeout")
        return frozenset(keys)


POLICY_CEILINGS: Mapping[str, PolicyCeiling] = {
    "db_control": PolicyCeiling(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=5,
        start_to_close_timeout=timedelta(seconds=60),
    ),
    "long_compute": PolicyCeiling(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=3,
        start_to_close_timeout=timedelta(hours=2),
        heartbeat_timeout=timedelta(seconds=30),
    ),
    "provider_managed_retry": PolicyCeiling(
        maximum_attempts=1,
        start_to_close_timeout=timedelta(minutes=10),
        single_attempt=True,
    ),
    "governed_external_write": PolicyCeiling(
        maximum_attempts=1,
        start_to_close_timeout=timedelta(minutes=2),
        single_attempt=True,
    ),
    "ledger_append": PolicyCeiling(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=10),
        maximum_attempts=5,
        start_to_close_timeout=timedelta(seconds=60),
    ),
}


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    """A loaded policy: what an Activity invocation passes to Temporal."""

    name: str
    retry_policy: RetryPolicy
    start_to_close_timeout: timedelta
    heartbeat_timeout: timedelta | None = None


def _ceiling_duration(policy: str, key: str, value: timedelta, ceiling: timedelta) -> timedelta:
    if value > ceiling:
        raise RetryPolicyError(
            f"{policy}.{key} exceeds the section 12 ceiling; pack data may only tighten it"
        )
    return value


def _load_policy(name: str, ceiling: PolicyCeiling, raw: Any) -> ActivityPolicy:
    if not isinstance(raw, Mapping):
        raise RetryPolicyError(f"{name} must be a mapping of policy values")
    unknown = set(raw) - ceiling.permitted_keys
    if unknown:
        raise RetryPolicyError(f"{name} has unknown keys: {', '.join(sorted(unknown))}")
    missing = ceiling.permitted_keys - set(raw)
    if missing:
        raise RetryPolicyError(f"{name} is missing keys: {', '.join(sorted(missing))}")

    attempts = raw["maximum_attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise RetryPolicyError(f"{name}.maximum_attempts must be a positive integer")
    if ceiling.single_attempt and attempts != 1:
        raise RetryPolicyError(
            f"{name}.maximum_attempts must be exactly 1; Temporal never retries this policy"
        )
    if attempts > ceiling.maximum_attempts:
        raise RetryPolicyError(
            f"{name}.maximum_attempts exceeds the section 12 ceiling; "
            "pack data may only tighten it"
        )

    start_to_close = _ceiling_duration(
        name,
        "start_to_close_timeout",
        parse_duration(f"{name}.start_to_close_timeout", raw["start_to_close_timeout"]),
        ceiling.start_to_close_timeout,
    )

    retry_kwargs: dict[str, Any] = {"maximum_attempts": attempts}
    if ceiling.initial_interval is not None:
        retry_kwargs["initial_interval"] = _ceiling_duration(
            name,
            "initial_interval",
            parse_duration(f"{name}.initial_interval", raw["initial_interval"]),
            ceiling.initial_interval,
        )
    if ceiling.maximum_interval is not None:
        retry_kwargs["maximum_interval"] = _ceiling_duration(
            name,
            "maximum_interval",
            parse_duration(f"{name}.maximum_interval", raw["maximum_interval"]),
            ceiling.maximum_interval,
        )
    if ceiling.backoff_coefficient is not None:
        coefficient = raw["backoff_coefficient"]
        if isinstance(coefficient, bool) or not isinstance(coefficient, (int, float)):
            raise RetryPolicyError(f"{name}.backoff_coefficient must be a number")
        if coefficient < 1.0:
            raise RetryPolicyError(f"{name}.backoff_coefficient must be at least 1.0")
        if coefficient > ceiling.backoff_coefficient:
            raise RetryPolicyError(
                f"{name}.backoff_coefficient exceeds the section 12 ceiling; "
                "pack data may only tighten it"
            )
        retry_kwargs["backoff_coefficient"] = float(coefficient)

    heartbeat: timedelta | None = None
    if ceiling.heartbeat_timeout is not None:
        heartbeat = _ceiling_duration(
            name,
            "heartbeat_timeout",
            parse_duration(f"{name}.heartbeat_timeout", raw["heartbeat_timeout"]),
            ceiling.heartbeat_timeout,
        )

    return ActivityPolicy(
        name=name,
        retry_policy=RetryPolicy(**retry_kwargs),
        start_to_close_timeout=start_to_close,
        heartbeat_timeout=heartbeat,
    )


def _default_path() -> Path:
    return Path(__file__).resolve().parents[2] / "packs" / "motor" / "orchestration.yaml"


@functools.lru_cache(maxsize=8)
def load_retry_policies(path: Path | None = None) -> Mapping[str, ActivityPolicy]:
    """Load and ceiling-check `packs/motor/orchestration.yaml`.

    Raises:
        RetryPolicyError: the file is unreadable, has unknown or missing keys,
            declares an invalid duration, or widens any section 12 ceiling.
    """

    source = path or _default_path()
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RetryPolicyError(f"orchestration pack data is unreadable: {error}") from error

    if not isinstance(document, Mapping):
        raise RetryPolicyError("orchestration pack data must be a mapping")
    unknown = set(document) - _TOP_LEVEL_KEYS
    if unknown:
        raise RetryPolicyError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    missing = _TOP_LEVEL_KEYS - set(document)
    if missing:
        raise RetryPolicyError(f"missing top-level keys: {', '.join(sorted(missing))}")
    if document["version"] != _SUPPORTED_VERSION:
        raise RetryPolicyError("orchestration pack data must declare version 1")

    raw_policies = document["retry_policies"]
    if not isinstance(raw_policies, Mapping):
        raise RetryPolicyError("retry_policies must be a mapping")
    declared = set(raw_policies)
    expected = set(POLICY_CEILINGS)
    if declared != expected:
        surplus = ", ".join(sorted(declared - expected))
        absent = ", ".join(sorted(expected - declared))
        detail = f"unknown: {surplus}" if surplus else f"missing: {absent}"
        raise RetryPolicyError(f"retry_policies must declare exactly the section 12 set ({detail})")

    return {
        name: _load_policy(name, ceiling, raw_policies[name])
        for name, ceiling in POLICY_CEILINGS.items()
    }
