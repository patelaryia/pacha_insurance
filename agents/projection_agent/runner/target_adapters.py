"""ICON and EDMS adapter slots plus the deployment target registry.

Every production target record in `infra/rpa_runner/targets.yaml` is
`blocked_on_inputs`: no ICON or EDMS endpoint, credential reference, or click
path has been captured. A blocked record refuses adapter construction outright,
so there is no code path that could reach a real system by accident (#296).

`base_url` and `secret_ref` are *references*. A live target must use HTTPS, sit
inside the runner's outbound allowlist, and name an AWS Secrets Manager
reference; a literal secret in this file is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from projection_agent.adapters import AdapterHealth, AdapterUnavailable, OpResult
from projection_agent.config import ClickPath
from projection_agent.runner.browser import (
    BrowserSession,
    ExactExecutor,
    ReflectionTimeout,
    SelectorDrift,
    StepFrame,
    TargetKnownFailure,
)

TARGET_KEYS = frozenset({"status", "blocked_on", "base_url", "secret_ref"})
TARGET_STATUSES = frozenset({"live", "blocked_on_inputs"})
SECRET_REF_PREFIX = "arn:aws:secretsmanager:"


class TargetRegistryError(ValueError):
    """The deployment target registry is unusable; construction fails closed."""


@dataclass(frozen=True)
class TargetRecord:
    """One environment's connectivity slot for a target system."""

    system: str
    status: str
    blocked_on: str | None
    base_url: str | None
    secret_ref: str | None

    @property
    def is_live(self) -> bool:
        return self.status == "live"


@dataclass(frozen=True)
class TargetRegistry:
    """The parsed `targets.yaml`. Fixtures inject their own, never this one."""

    systems: dict[str, TargetRecord]

    def require(self, system: str) -> TargetRecord:
        record = self.systems.get(system)
        if record is None:
            raise AdapterUnavailable(system, "target_not_registered")
        if not record.is_live:
            raise AdapterUnavailable(system, record.blocked_on or "blocked_on_inputs")
        return record


def load_target_registry(path: Path) -> TargetRegistry:
    """Parse and validate the deployment target registry."""

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise TargetRegistryError(f"invalid target registry: {error}") from error
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise TargetRegistryError("target registry requires version 1")
    if set(raw) != {"version", "systems"}:
        raise TargetRegistryError("target registry keys are invalid")
    systems = raw.get("systems")
    if not isinstance(systems, dict) or not systems:
        raise TargetRegistryError("target registry requires a systems mapping")
    records: dict[str, TargetRecord] = {}
    for system, entry in systems.items():
        if not isinstance(entry, dict) or set(entry) != TARGET_KEYS:
            raise TargetRegistryError(f"target {system!r} keys are invalid")
        status = entry.get("status")
        if status not in TARGET_STATUSES:
            raise TargetRegistryError(f"target {system!r} status {status!r} is unknown")
        base_url = entry.get("base_url")
        secret_ref = entry.get("secret_ref")
        blocked_on = entry.get("blocked_on")
        if status == "live":
            if not isinstance(base_url, str) or not base_url.startswith("https://"):
                raise TargetRegistryError(f"target {system!r} must use an HTTPS endpoint")
            if not isinstance(secret_ref, str) or not secret_ref.startswith(SECRET_REF_PREFIX):
                raise TargetRegistryError(
                    f"target {system!r} must name a Secrets Manager reference, not a value"
                )
            if blocked_on is not None:
                raise TargetRegistryError(f"live target {system!r} carries a blocker")
        else:
            if base_url is not None or secret_ref is not None:
                raise TargetRegistryError(
                    f"blocked target {system!r} must not carry an endpoint or secret reference"
                )
            if not isinstance(blocked_on, str) or not blocked_on:
                raise TargetRegistryError(f"blocked target {system!r} needs a blocker")
        records[str(system)] = TargetRecord(
            system=str(system),
            status=str(status),
            blocked_on=blocked_on if isinstance(blocked_on, str) else None,
            base_url=base_url if isinstance(base_url, str) else None,
            secret_ref=secret_ref if isinstance(secret_ref, str) else None,
        )
    return TargetRegistry(systems=records)


class TargetAdapter:
    """One target system's adapter, backed by isolated browser contexts.

    `execute` is called only by ``agent_runtime.gate.execute_authorised_adapter``
    and only with a currently leased work receipt.
    """

    def __init__(
        self,
        system: Literal["icon", "edms", "finance"],
        *,
        session_factory: Callable[[], BrowserSession],
        timeouts: Any,
        clock: Callable[[], Any],
        runner_id: str,
        probe: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        on_frame: Callable[[StepFrame], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.system = system
        self._session_factory = session_factory
        self._timeouts = timeouts
        self._clock = clock
        self._runner_id = runner_id
        self._probe = probe
        self._on_frame = on_frame
        self._heartbeat = heartbeat
        self._sleep = sleep
        self._click_paths: dict[str, ClickPath] = {}

    def register(self, click_path: ClickPath) -> None:
        """Bind the resolved definition the control API handed the runner."""

        self._click_paths[click_path.operation] = click_path

    # -- Adapter protocol ------------------------------------------------------

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            status="healthy",
            checked_at=self._clock(),
            system=self.system,
            runner_id=self._runner_id,
            reason_code=None,
        )

    def execute(self, op: Any, payload: dict[str, object], run_id: str) -> OpResult:
        """Drive one isolated context exactly, and always close it."""

        operation_id = op if isinstance(op, str) else getattr(op, "id", "")
        click_path = self._click_paths.get(str(operation_id))
        if click_path is None:
            return OpResult(
                outcome="known_failure",
                last_completed_step=None,
                write_ids=(),
                readback_keys={},
                evidence_ids=(),
                reason_code="definition_not_registered",
            )
        values = {
            str(key): str(value)
            for key, value in (payload.get("values") or {}).items()  # type: ignore[union-attr]
        }
        session = self._session_factory()
        executor = ExactExecutor(
            session,
            click_path,
            timeouts=self._timeouts,
            heartbeat=self._heartbeat,
            on_frame=self._on_frame,
            **({"sleep": self._sleep} if self._sleep is not None else {}),
        )
        try:
            trace = executor.run(values)
            outputs = executor.read_outputs()
            inputs = executor.read_inputs()
        except SelectorDrift as drift:
            return OpResult(
                outcome="ui_drift",
                last_completed_step=executor.trace.last_completed_step,
                write_ids=tuple(executor.trace.write_ids),
                readback_keys={},
                evidence_ids=(),
                reason_code=drift.reason_code,
            )
        except TargetKnownFailure as failure:
            handler = click_path.handler_for(failure.signature)
            if handler == "duplicate_filename":
                claim_id = payload.get("claim_id")
                if not isinstance(claim_id, str) or len(claim_id) < 6:
                    return OpResult(
                        outcome="known_failure",
                        last_completed_step=executor.trace.last_completed_step,
                        write_ids=tuple(executor.trace.write_ids),
                        readback_keys={},
                        evidence_ids=(),
                        reason_code="duplicate_filename_claim_id_missing",
                    )
                try:
                    trace = executor.retry_duplicate_filename(
                        failure.step_id, claim_id_suffix=claim_id[-6:].upper()
                    )
                    outputs = executor.read_outputs()
                    inputs = executor.read_inputs()
                except TargetKnownFailure:
                    return OpResult(
                        outcome="known_failure",
                        last_completed_step=executor.trace.last_completed_step,
                        write_ids=tuple(executor.trace.write_ids),
                        readback_keys={},
                        evidence_ids=(),
                        reason_code="edms_duplicate_filename_collision",
                    )
                except ReflectionTimeout:
                    return OpResult(
                        outcome="uncertain_write",
                        last_completed_step=executor.trace.last_completed_step,
                        write_ids=tuple(executor.trace.write_ids),
                        readback_keys={},
                        evidence_ids=(),
                        reason_code="slow_reflection_timeout",
                    )
                except SelectorDrift as drift:
                    return OpResult(
                        outcome="ui_drift",
                        last_completed_step=executor.trace.last_completed_step,
                        write_ids=tuple(executor.trace.write_ids),
                        readback_keys={},
                        evidence_ids=(),
                        reason_code=drift.reason_code,
                    )
                else:
                    return OpResult(
                        outcome="submitted",
                        last_completed_step=trace.last_completed_step,
                        write_ids=tuple(trace.write_ids),
                        readback_keys={"inputs": inputs, "outputs": outputs},
                        evidence_ids=(),
                        reason_code=None,
                    )
            return OpResult(
                outcome="known_failure" if handler else "uncertain_write",
                last_completed_step=executor.trace.last_completed_step,
                write_ids=tuple(executor.trace.write_ids),
                readback_keys={},
                evidence_ids=(),
                reason_code=handler or "unmapped_target_signature",
            )
        except ReflectionTimeout:
            return OpResult(
                outcome="uncertain_write",
                last_completed_step=executor.trace.last_completed_step,
                write_ids=tuple(executor.trace.write_ids),
                readback_keys={},
                evidence_ids=(),
                reason_code="slow_reflection_timeout",
            )
        except Exception:  # noqa: BLE001 - an unknown response is never a guess
            return OpResult(
                outcome="uncertain_write",
                last_completed_step=executor.trace.last_completed_step,
                write_ids=tuple(executor.trace.write_ids),
                readback_keys={},
                evidence_ids=(),
                reason_code="unknown_target_response",
            )
        finally:
            # The context is closed on success, on target failure, on callback
            # failure, and on cancellation. It is never reused.
            session.close()
        del run_id
        return OpResult(
            outcome="submitted",
            last_completed_step=trace.last_completed_step,
            write_ids=tuple(trace.write_ids),
            readback_keys={"inputs": inputs, "outputs": outputs},
            evidence_ids=(),
            reason_code=None,
        )

    def readback(self, op: Any, keys: dict[str, object]) -> dict[str, object]:
        """Run the captured prior-completion probe. Never a write."""

        if self._probe is None:
            raise AdapterUnavailable(self.system, "probe_not_configured")
        operation_id = op if isinstance(op, str) else getattr(op, "id", "")
        return self._probe(str(operation_id), dict(keys))


__all__ = [
    "TargetAdapter",
    "TargetRecord",
    "TargetRegistry",
    "TargetRegistryError",
    "load_target_registry",
]
