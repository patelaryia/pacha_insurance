"""Governed RPA coordinator: authorisation, leases, evidence, and recovery.

PACKET-21 §§2/6/7/8/9/11. Everything here is control plane. No browser is
opened, no selector is read, and no credential is resolved: the runner process
does that, out of band, and reports back through the authenticated control API.

The invariants this module exists to hold:

* an external write is authorised only through the AR-2 gate, and only as a
  *deferred* action, so authorisation and the reconciled outcome share one
  durable `agent_run` (register #278);
* at most one live lease exists per projection, and a lease is a concurrency
  boundary, never permission to blind-retry (register #283);
* a possible write without exact readback proof is `uncertain_write` and
  terminal — it is never offered as a paste fallback;
* every configuration change is data, and the only runtime override is a
  versioned circuit breaker in `platform_state` (register #280).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from agent_runtime import Action, DeferredAction, WorkReceipt
from claim_core import ClaimCoreError, new_ulid
from claim_core.models import PlatformState
from projection_agent.adapters import AdapterHealth, AdapterUnavailable, OpResult
from projection_agent.config import Operation, OperationConfigError
from projection_agent.models import Projection
from projection_agent.paste import encode_copy_value

#: The one registered deferred action type (PACKET-21 §6).
RPA_ACTION_TYPE = "projection.rpa.execute"
#: The runner's own platform identity. Never a person, never a target login.
RUNNER_ACTOR = "agent:projection-runner"
#: The DRAFT_RELEASE subtype that confirms one L2 RPA launch.
RPA_SUBTYPE = "projection_rpa"
#: The coarse COP stages every `project.*` capability declares (PACKET-21 §6).
COP_STAGES: tuple[str, ...] = ("authorise", "lease", "execute", "readback", "reconcile")
#: Exact `platform_state` key shapes (PACKET-21 §7).
RUNNER_STATE_KEY = "projection.runner.{runner_id}"
CIRCUIT_STATE_KEY = "projection.circuit.{operation}"
#: Server-owned evidence key shape. The API never returns a raw blob key.
EVIDENCE_KEY = "projection-evidence/{claim_id}/{projection_id}/rpa/{attempt}/{sequence}.png"

L2_OR_ABOVE = ("L2", "L3", "L4")


class RunnerAuthenticator:
    """The injected machine-identity check for the internal runner routes.

    PACKET-21 does not choose mTLS, Entra client credentials, or any other
    scheme: infra supplies a concrete authenticator in PACKET-22. Until then the
    routes are simply not mounted (register #277).
    """

    def authenticate(self, headers: dict[str, str]) -> str:
        """Return the authenticated runner id, or raise ``ClaimCoreError``."""

        raise NotImplementedError


@dataclass(frozen=True)
class LeaseGrant:
    """The one-time response to an authorised runner job claim."""

    projection_id: str
    claim_id: str
    operation: str
    definition_version: str
    run_id: str
    attempt: int
    runner_id: str
    lease_token: str
    expires_at: datetime
    receipt: WorkReceipt

    def as_dict(self, *, definition: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "claim_id": self.claim_id,
            "operation": self.operation,
            "definition_version": self.definition_version,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "runner_id": self.runner_id,
            # Returned exactly once, over TLS. Only its SHA-256 is stored.
            "lease_token": self.lease_token,
            "expires_at": self.expires_at.isoformat(),
            "definition": definition,
            "payload": payload,
        }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return None if aware is None else aware.isoformat()


class RpaCoordinator:
    """Owns every RPA state change for one application."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.app = service.app
        self.operations = service.operations
        self.runtime = service.operations.runtime
        self.clock = service.clock
        self.adapters: dict[str, Any] = {}

    # -- durable control state -------------------------------------------------

    def _state(self, key: str) -> dict[str, Any] | None:
        with self.service.sessions() as session:
            row = session.get(PlatformState, key)
            if row is None:
                return None
            value = row.value
        return dict(value) if isinstance(value, dict) else None

    def _put_state(self, key: str, value: dict[str, Any]) -> None:
        now = self.clock()
        with self.service.sessions.begin() as session:
            row = session.get(PlatformState, key)
            if row is None:
                session.add(PlatformState(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

    def circuit(self, operation_id: str) -> dict[str, Any] | None:
        """Return the open circuit breaker for one operation, if any.

        An unknown key shape or a version mismatch is ignored for execution and
        reported as unavailable — never treated as healthy.
        """

        state = self._state(CIRCUIT_STATE_KEY.format(operation=operation_id))
        if state is None or state.get("status") != "open":
            return None
        if state.get("operation") != operation_id or not isinstance(
            state.get("definition_version"), str
        ):
            return {
                "status": "open",
                "operation": operation_id,
                "definition_version": None,
                "reason_code": "circuit_state_unreadable",
                "opened_at": state.get("opened_at"),
            }
        return state

    def open_circuit(
        self, operation_id: str, *, definition_version: str, reason_code: str, actor: str
    ) -> None:
        """Durably stop new RPA execution for one operation version."""

        self._put_state(
            CIRCUIT_STATE_KEY.format(operation=operation_id),
            {
                "status": "open",
                "operation": operation_id,
                "definition_version": definition_version,
                "reason_code": reason_code,
                "opened_at": self.clock().isoformat(),
                "opened_by": actor,
            },
        )

    def clear_circuit(self, operation_id: str, *, actor: str) -> dict[str, Any]:
        """Clear a circuit only when the qualification in §11 is met."""

        if operation_id not in self.operations:
            raise ClaimCoreError(404, "UNKNOWN_OPERATION", "Operation was not found")
        state = self.circuit(operation_id)
        if state is None:
            raise ClaimCoreError(
                409, "PROJECTION_CIRCUIT_NOT_OPEN", "No circuit breaker is open"
            )
        operation = self.operations.get(operation_id)
        installed = (
            operation.click_path.version if operation.click_path is not None else None
        )
        opened_version = state.get("definition_version")
        if installed is None or not isinstance(opened_version, str):
            raise ClaimCoreError(
                409,
                "PROJECTION_CIRCUIT_BLOCKED",
                "A strictly newer operation definition must be installed first",
            )
        if _version_tuple(installed) <= _version_tuple(opened_version):
            raise ClaimCoreError(
                409,
                "PROJECTION_CIRCUIT_BLOCKED",
                "A strictly newer operation definition must be installed first",
            )
        health = self.adapter_health(operation.system, ignore_circuits=True)
        if health.status != "healthy":
            raise ClaimCoreError(
                409,
                "PROJECTION_CIRCUIT_BLOCKED",
                "The system adapter is not healthy",
            )
        self._put_state(
            CIRCUIT_STATE_KEY.format(operation=operation_id),
            {
                "status": "cleared",
                "operation": operation_id,
                "definition_version": installed,
                "cleared_at": self.clock().isoformat(),
                "cleared_by": actor,
                "previous_version": opened_version,
            },
        )
        with self.service.sessions.begin() as session:
            self.service._emit(
                session,
                claim_id=None,
                event_type="agent.action_logged",
                payload={
                    "capability_id": f"project.{operation_id}",
                    "action_type": "projection.circuit.cleared",
                    "operation": operation_id,
                    "definition_version": installed,
                    "previous_version": opened_version,
                },
                actor=actor,
            )
        return {
            "operation": operation_id,
            "status": "cleared",
            "definition_version": installed,
        }

    def record_runner_heartbeat(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the runner's closed health codes. Never a value or a selector."""

        state = {
            "runner_id": runner_id,
            "last_seen_at": self.clock().isoformat(),
            "runner_version": payload.get("runner_version"),
            "browser_version": payload.get("browser_version"),
            "systems": sorted(payload.get("systems") or ()),
            "health": payload.get("health"),
        }
        self._put_state(RUNNER_STATE_KEY.format(runner_id=runner_id), state)
        return state

    def runners(self) -> list[dict[str, Any]]:
        with self.service.sessions() as session:
            rows = list(
                session.scalars(
                    select(PlatformState).where(
                        PlatformState.key.like("projection.runner.%")
                    )
                )
            )
            values = [dict(row.value) for row in rows if isinstance(row.value, dict)]
        return sorted(values, key=lambda row: str(row.get("runner_id")))

    # -- adapter health --------------------------------------------------------

    def adapter_health(self, system: str, *, ignore_circuits: bool = False) -> AdapterHealth:
        """Derive one system's health from configuration, runner, probe, circuit."""

        now = self.clock()
        open_circuits = [] if ignore_circuits else self.open_circuit_ids(system)
        if open_circuits:
            return AdapterHealth(
                status="circuit_open",
                checked_at=now,
                system=system,
                runner_id=None,
                reason_code="ui_drift",
            )
        adapter = self.adapters.get(system)
        if adapter is None:
            return AdapterHealth(
                status="unavailable",
                checked_at=now,
                system=system,
                runner_id=None,
                reason_code="pending_capture",
            )
        try:
            health = adapter.health()
        except AdapterUnavailable as error:
            return AdapterHealth(
                status="unavailable",
                checked_at=now,
                system=system,
                runner_id=None,
                reason_code=error.reason_code,
            )
        seen = [
            row
            for row in self.runners()
            if system in (row.get("systems") or ()) and row.get("last_seen_at")
        ]
        if health.status == "healthy" and not seen:
            return AdapterHealth(
                status="degraded",
                checked_at=now,
                system=system,
                runner_id=None,
                reason_code="no_runner_heartbeat",
            )
        return health

    def open_circuit_ids(self, system: str | None = None) -> list[str]:
        ids = []
        for operation in self.operations.all():
            if system is not None and operation.system != system:
                continue
            if self.circuit(operation.id) is not None:
                ids.append(operation.id)
        return ids

    def systems_health(self) -> list[dict[str, Any]]:
        """The S-6 adapter/control rows. No credential or selector is returned."""

        rows: list[dict[str, Any]] = []
        for system in ("icon", "edms"):
            operations = [
                operation for operation in self.operations.all() if operation.system == system
            ]
            configured = sorted({operation.mode for operation in operations})
            health = self.adapter_health(system)
            circuits = self.open_circuit_ids(system)
            effective = "paste_assist" if health.status != "healthy" else (
                "rpa" if any(operation.is_live_rpa for operation in operations) else "paste_assist"
            )
            last_seen = [
                row.get("last_seen_at")
                for row in self.runners()
                if system in (row.get("systems") or ())
            ]
            rows.append(
                {
                    "system": system,
                    "configured_mode": configured[0] if len(configured) == 1 else "mixed",
                    "effective_mode": effective,
                    "status": health.status,
                    "reason_code": health.reason_code,
                    "runner_last_seen_at": max(last_seen) if last_seen else None,
                    "circuit_operation_ids": circuits,
                }
            )
        return rows

    # -- authorisation ---------------------------------------------------------

    def assert_activation(self, operation: Operation) -> None:
        """Refuse a live RPA row that does not meet every §4 activation condition."""

        if not operation.is_live_rpa:
            return
        if operation.owner_prd != "PRD-09":
            raise ClaimCoreError(
                409,
                "PROJECTION_RPA_NOT_ACTIVATABLE",
                f"{operation.id} is owned by {operation.owner_prd}",
            )
        level = self.service.capability_level(operation.capability_id)
        if level not in L2_OR_ABOVE:
            raise ClaimCoreError(
                409,
                "PROJECTION_RPA_NOT_ACTIVATABLE",
                f"{operation.capability_id} is {level}; a live RPA definition starts at L2",
            )
        if operation.system not in self.adapters:
            raise ClaimCoreError(
                409,
                "PROJECTION_RPA_NOT_ACTIVATABLE",
                f"No {operation.system} adapter is installed",
            )
        if self.service.runner_authenticator is None:
            raise ClaimCoreError(
                409,
                "PROJECTION_RPA_NOT_ACTIVATABLE",
                "No runner authenticator is installed",
            )
        if self.circuit(operation.id) is not None:
            raise ClaimCoreError(
                409, "PROJECTION_RPA_NOT_ACTIVATABLE", "A circuit breaker is open"
            )

    def authorise(self, projection_id: str, *, actor: str) -> dict[str, Any]:
        """Take a queued RPA row through the AR-2 gate exactly once."""

        row = self.service._row(projection_id)
        operation = self.operations.get(row.operation)
        if row.mode != "rpa":
            raise ClaimCoreError(
                409, "PROJECTION_MODE_NOT_RPA", "This projection is not an RPA row"
            )
        self.assert_activation(operation)
        evidence = self.rpa_evidence(row)
        if evidence.get("authorisation") is not None or evidence.get("gate") is not None:
            return {"status": "exists", "run_id": evidence.get("run_id")}
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by assert_activation
        payload = row.payload if isinstance(row.payload, dict) else {}
        run_id = self.app.state.agent_runtime.start_run(
            agent="projection",
            capability_id=operation.capability_id,
            claim_id=row.claim_id,
        )
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            current = self.rpa_evidence(locked)
            current["run_id"] = run_id
            self.store_rpa_evidence(locked, current)
        outcome = self.app.state.agent_runtime.execute_or_stage(
            capability_id=operation.capability_id,
            action=Action(
                type=RPA_ACTION_TYPE,
                # Exactly the four PACKET-21 §6 keys: no target value, encrypted
                # envelope, selector, credential ref, or blob key.
                payload={
                    "projection_id": projection_id,
                    "operation": operation.id,
                    "definition_version": click_path.version,
                    "snapshot_hash": payload.get("snapshot_hash"),
                },
            ),
            claim_id=row.claim_id,
            actor=actor,
            run_id=run_id,
        )
        if outcome["status"] == "staged":
            with self.service._guard(projection_id) as session:
                locked = session.get(Projection, projection_id)
                if locked is not None:
                    current = self.rpa_evidence(locked)
                    current["gate"] = {
                        "review_id": outcome.get("review_id"),
                        "review_type": outcome.get("review_type"),
                        "run_id": run_id,
                        "staged_at": self.clock().isoformat(),
                    }
                    self.store_rpa_evidence(locked, current)
        return {**outcome, "run_id": run_id}

    def _deferred_executor(self, action: Action, run_id: str) -> DeferredAction:
        """Authorise, but do not perform, one RPA write."""

        projection_id = action.payload.get("projection_id")
        if not isinstance(projection_id, str):
            raise ClaimCoreError(
                422, "PROJECTION_ACTION_INVALID", "A projection id is required"
            )
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            payload = locked.payload if isinstance(locked.payload, dict) else {}
            if (
                locked.operation != action.payload.get("operation")
                or payload.get("snapshot_hash") != action.payload.get("snapshot_hash")
            ):
                raise ClaimCoreError(
                    409, "PROJECTION_STATE_STALE", "The staged projection no longer matches"
                )
            if locked.status != "queued" or locked.mode != "rpa":
                raise ClaimCoreError(
                    409,
                    "PROJECTION_STATE_STALE",
                    f"An RPA job requires a queued rpa row, not {locked.status}/{locked.mode}",
                )
            evidence = self.rpa_evidence(locked)
            evidence["run_id"] = run_id
            evidence["authorisation"] = {
                "run_id": run_id,
                "authorised_at": self.clock().isoformat(),
                "definition_version": action.payload.get("definition_version"),
                "snapshot_hash": action.payload.get("snapshot_hash"),
            }
            self.store_rpa_evidence(locked, evidence)
        return DeferredAction(run_id=run_id, ref={"projection_id": projection_id})

    def consume_review(self, payload: dict[str, Any], *, actor: str) -> None:
        """Apply one resolved `DRAFT_RELEASE{projection_rpa}` decision, idempotently."""

        review_id = payload.get("review_id")
        if payload.get("type") != "DRAFT_RELEASE" or not isinstance(review_id, str):
            return
        item = self.service.review_item(review_id)
        if item is None or item.get("subtype") != RPA_SUBTYPE:
            return
        action = (item.get("payload") or {}).get("action") or {}
        staged = action.get("payload") if isinstance(action, dict) else None
        if not isinstance(staged, dict):
            return
        projection_id = staged.get("projection_id")
        run_id = (item.get("payload") or {}).get("agent_run_id")
        if not isinstance(projection_id, str) or not isinstance(run_id, str):
            return
        resolution = payload.get("resolution")
        if resolution == "approved":
            self.app.state.agent_runtime.execute_staged(
                Action(type=RPA_ACTION_TYPE, payload=dict(staged)), run_id=run_id
            )
            return
        # Edit→Approve and Reject both launch nothing. The row becomes available
        # in paste-assist; the RPA attempt evidence is retained.
        self.fall_back_to_paste(
            projection_id,
            reason_code="draft_release_" + str(resolution),
            actor=actor,
            run_id=run_id,
        )

    def fall_back_to_paste(
        self, projection_id: str, *, reason_code: str, actor: str, run_id: str | None = None
    ) -> None:
        """Return one row to paste-assist without touching the target or the pack."""

        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None or locked.status in {"failed", "diverged", "completed"}:
                return
            evidence = self.rpa_evidence(locked)
            if evidence.get("fallback") is not None:
                return
            if locked.status == "executing":
                self.service._transition(locked, "queued")
            locked.mode = "paste_assist"
            evidence["fallback"] = {
                "reason_code": reason_code,
                "at": self.clock().isoformat(),
                "actor": actor,
            }
            evidence.pop("lease", None)
            self.store_rpa_evidence(locked, evidence)
        if run_id is not None:
            self.finish_run(
                run_id,
                status="completed",
                stages=("authorise",),
                outcome={"result": "fallback_to_paste", "reason_code": reason_code},
            )

    # -- durable evidence ------------------------------------------------------

    @staticmethod
    def rpa_evidence(row: Projection) -> dict[str, Any]:
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        rpa = evidence.get("rpa")
        return dict(rpa) if isinstance(rpa, dict) else {}

    @staticmethod
    def store_rpa_evidence(row: Projection, rpa: dict[str, Any]) -> None:
        evidence = dict(row.evidence) if isinstance(row.evidence, dict) else {}
        evidence["rpa"] = rpa
        row.evidence = evidence

    @staticmethod
    def _attempt_record(rpa: dict[str, Any], attempt: int) -> dict[str, Any]:
        for record in rpa.get("attempts") or ():
            if isinstance(record, dict) and record.get("attempt") == attempt:
                return record
        return {}

    def _upsert_attempt(self, rpa: dict[str, Any], record: dict[str, Any]) -> None:
        attempts = [
            dict(item)
            for item in (rpa.get("attempts") or ())
            if isinstance(item, dict) and item.get("attempt") != record["attempt"]
        ]
        attempts.append(record)
        rpa["attempts"] = sorted(attempts, key=lambda item: int(item["attempt"]))

    # -- lease -----------------------------------------------------------------

    def claim_job(self, *, runner_id: str, systems: tuple[str, ...]) -> LeaseGrant | None:
        """Grant at most one lease for the oldest authorised queued row."""

        declared = tuple(system for system in systems if system in {"icon", "edms"})
        if not declared:
            return None
        with self.service.sessions() as session:
            candidates = list(
                session.scalars(
                    select(Projection)
                    .where(Projection.status == "queued", Projection.mode == "rpa")
                    .order_by(Projection.created_at, Projection.id)
                )
            )
            for row in candidates:
                session.expunge(row)
        for candidate in candidates:
            operation = (
                self.operations.get(candidate.operation)
                if candidate.operation in self.operations
                else None
            )
            if operation is None or operation.system not in declared:
                continue
            grant = self._grant(candidate.id, runner_id=runner_id, operation=operation)
            if grant is not None:
                return grant
        return None

    def _grant(
        self, projection_id: str, *, runner_id: str, operation: Operation
    ) -> LeaseGrant | None:
        if self.circuit(operation.id) is not None:
            return None
        if self.adapter_health(operation.system).status != "healthy":
            return None
        timings = self.runtime.runner
        now = self.clock()
        expires_at = now + timedelta(seconds=timings.lease_seconds)
        token = secrets.token_hex(32)
        click_path = operation.click_path
        if click_path is None:
            return None
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None or locked.status != "queued" or locked.mode != "rpa":
                return None
            rpa = self.rpa_evidence(locked)
            authorisation = rpa.get("authorisation")
            run_id = rpa.get("run_id")
            if not isinstance(authorisation, dict) or not isinstance(run_id, str):
                return None
            if self._live_lease(rpa, now) is not None:
                return None
            attempt = int(locked.attempts or 0) + 1
            if attempt > timings.max_attempts:
                return None
            locked.attempts = attempt
            rpa["lease"] = {
                # Only the digest is durable; the raw token never touches storage.
                "token_sha256": _sha256(token),
                "runner_id": runner_id,
                "attempt": attempt,
                "run_id": run_id,
                "leased_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            self._upsert_attempt(
                rpa,
                {
                    **self._attempt_record(rpa, attempt),
                    "attempt": attempt,
                    "runner_id": runner_id,
                    "leased_at": now.isoformat(),
                    "ended_at": None,
                    "last_completed_step": None,
                    "write_ids": [],
                    "outcome": None,
                    "frames": self._attempt_record(rpa, attempt).get("frames", []),
                },
            )
            self.service._transition(locked, "executing")
            self.store_rpa_evidence(locked, rpa)
            claim_id = locked.claim_id
        return LeaseGrant(
            projection_id=projection_id,
            claim_id=claim_id,
            operation=operation.id,
            definition_version=click_path.version,
            run_id=run_id,
            attempt=attempt,
            runner_id=runner_id,
            lease_token=token,
            expires_at=expires_at,
            receipt=WorkReceipt(
                run_id=run_id,
                projection_id=projection_id,
                operation=operation.id,
                definition_version=click_path.version,
                attempt=attempt,
                expires_at=expires_at,
                lease_token_sha256=_sha256(token),
            ),
        )

    def _live_lease(self, rpa: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        lease = rpa.get("lease")
        if not isinstance(lease, dict):
            return None
        expires_at = lease.get("expires_at")
        if not isinstance(expires_at, str):
            return None
        if _aware(datetime.fromisoformat(expires_at)) <= _aware(now):
            return None
        return lease

    def verify_lease(self, row: Projection, token: str) -> dict[str, Any]:
        """Constant-time lease check. A stale or cross-run callback mutates nothing."""

        rpa = self.rpa_evidence(row)
        lease = rpa.get("lease")
        if not isinstance(lease, dict) or not isinstance(token, str) or not token:
            raise ClaimCoreError(404, "PROJECTION_LEASE_NOT_FOUND", "No lease is held")
        if not secrets.compare_digest(str(lease.get("token_sha256")), _sha256(token)):
            raise ClaimCoreError(404, "PROJECTION_LEASE_NOT_FOUND", "No lease is held")
        if int(lease.get("attempt", 0)) != int(row.attempts or 0):
            raise ClaimCoreError(
                409, "PROJECTION_LEASE_STALE", "That lease belongs to another attempt"
            )
        expires_at = lease.get("expires_at")
        if not isinstance(expires_at, str) or _aware(
            datetime.fromisoformat(expires_at)
        ) <= _aware(self.clock()):
            raise ClaimCoreError(409, "PROJECTION_LEASE_STALE", "That lease has expired")
        return lease

    def heartbeat(self, projection_id: str, *, token: str, runner_id: str) -> dict[str, Any]:
        """Extend one live lease by exactly `lease_seconds`. Nothing else changes."""

        expires_at = self.clock() + timedelta(seconds=self.runtime.runner.lease_seconds)
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            lease = self.verify_lease(locked, token)
            if lease.get("runner_id") != runner_id:
                raise ClaimCoreError(
                    409, "PROJECTION_LEASE_STALE", "That lease belongs to another runner"
                )
            rpa = self.rpa_evidence(locked)
            rpa["lease"] = {**lease, "expires_at": expires_at.isoformat()}
            self.store_rpa_evidence(locked, rpa)
        return {"projection_id": projection_id, "expires_at": expires_at.isoformat()}

    # -- evidence frames -------------------------------------------------------

    def record_frame(
        self,
        projection_id: str,
        *,
        token: str,
        runner_id: str,
        step_id: str,
        phase: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Store one before/after/failure screenshot against the current attempt."""

        if phase not in {"before", "after", "failure"}:
            raise ClaimCoreError(422, "EVIDENCE_PHASE_UNKNOWN", "Unknown evidence phase")
        digest = hashlib.sha256(content).hexdigest()
        evidence_id = new_ulid()
        captured_at = self.clock()
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            lease = self.verify_lease(locked, token)
            if lease.get("runner_id") != runner_id:
                raise ClaimCoreError(
                    409, "PROJECTION_LEASE_STALE", "That lease belongs to another runner"
                )
            attempt = int(lease["attempt"])
            rpa = self.rpa_evidence(locked)
            record = self._attempt_record(rpa, attempt)
            operation = self.operations.get(locked.operation)
            click_path = operation.click_path
            if click_path is None:
                raise ClaimCoreError(
                    409, "PROJECTION_DEFINITION_MISSING", "Projection definition is unavailable"
                )
            try:
                step = click_path.step(step_id)
            except OperationConfigError as error:
                raise ClaimCoreError(
                    422, "EVIDENCE_STEP_UNKNOWN", "Evidence step is not in the leased definition"
                ) from error
            frames = [dict(frame) for frame in record.get("frames", [])]
            sequence = len(frames) + 1
            key = EVIDENCE_KEY.format(
                claim_id=locked.claim_id,
                projection_id=projection_id,
                attempt=attempt,
                sequence=f"{sequence:04d}",
            )
            self.app.state.blob_store.put(key, content)
            frames.append(
                {
                    "evidence_id": evidence_id,
                    "step_id": step_id,
                    "phase": phase,
                    "sha256": digest,
                    "sequence": sequence,
                    "captured_at": captured_at.isoformat(),
                    # Server-owned; the API resolves it and never returns it.
                    "key": key,
                }
            )
            durable_writes = list(record.get("write_ids") or ())
            if step.write_id is not None and phase in {"before", "after", "failure"}:
                if step.write_id not in durable_writes:
                    durable_writes.append(step.write_id)
            durable_last = record.get("last_completed_step")
            if phase == "after":
                durable_last = step.id
            record = {
                **record,
                "attempt": attempt,
                "frames": frames,
                "last_completed_step": durable_last,
                "write_ids": durable_writes,
            }
            self._upsert_attempt(rpa, record)
            self.store_rpa_evidence(locked, rpa)
        return {"evidence_id": evidence_id, "sha256": digest, "sequence": sequence}

    def frames(self, row: Projection) -> list[dict[str, Any]]:
        rpa = self.rpa_evidence(row)
        collected: list[dict[str, Any]] = []
        for record in rpa.get("attempts") or ():
            if not isinstance(record, dict):
                continue
            for frame in record.get("frames") or ():
                if isinstance(frame, dict):
                    collected.append({**frame, "attempt": record.get("attempt")})
        return sorted(
            collected, key=lambda frame: (int(frame.get("attempt") or 0), int(frame["sequence"]))
        )

    def evidence_complete(self, row: Projection, result: OpResult) -> bool:
        """Every executed step needs one before and one after frame."""

        rpa = self.rpa_evidence(row)
        attempt = int(row.attempts or 0)
        record = self._attempt_record(rpa, attempt)
        phases: dict[str, set[str]] = {}
        for frame in record.get("frames") or ():
            if isinstance(frame, dict):
                phases.setdefault(str(frame.get("step_id")), set()).add(str(frame.get("phase")))
        if not phases:
            return False
        sequences = [
            int(frame["sequence"])
            for frame in record.get("frames") or ()
            if isinstance(frame, dict)
        ]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            return False
        executed = self._executed_steps(row, result)
        if not executed:
            return False
        return all({"before", "after"} <= phases.get(step_id, set()) for step_id in executed)

    def _executed_steps(self, row: Projection, result: OpResult) -> list[str]:
        operation = self.operations.get(row.operation)
        click_path = operation.click_path
        if click_path is None:
            return []
        ordered = [step.id for step in click_path.steps]
        if result.last_completed_step is None or result.last_completed_step not in ordered:
            return []
        if result.outcome in {"submitted", "completed_existing"}:
            if not ordered or result.last_completed_step != ordered[-1]:
                return []
        return ordered[: ordered.index(result.last_completed_step) + 1]

    # -- result ----------------------------------------------------------------

    def record_result(
        self, projection_id: str, *, token: str, runner_id: str, result: OpResult
    ) -> dict[str, Any]:
        """Accept one runner outcome and route it to the correct lifecycle edge."""

        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            lease = self.verify_lease(locked, token)
            if lease.get("runner_id") != runner_id:
                raise ClaimCoreError(
                    409, "PROJECTION_LEASE_STALE", "That lease belongs to another runner"
                )
            attempt = int(lease["attempt"])
            rpa = self.rpa_evidence(locked)
            record = self._attempt_record(rpa, attempt)
            if record.get("outcome") is not None:
                # An exact repeated callback returns the stored outcome.
                return {"status": locked.status, "outcome": record["outcome"], "replayed": True}
            durable_writes = list(record.get("write_ids") or ())
            for write_id in result.write_ids:
                if write_id not in durable_writes:
                    durable_writes.append(write_id)
            record = {
                **record,
                "attempt": attempt,
                "ended_at": self.clock().isoformat(),
                "last_completed_step": (
                    result.last_completed_step or record.get("last_completed_step")
                ),
                "write_ids": durable_writes,
                "outcome": result.outcome,
                "reason_code": result.reason_code,
            }
            self._upsert_attempt(rpa, record)
            rpa.pop("lease", None)
            if result.outcome in {"submitted", "completed_existing"}:
                self.service._transition(locked, "verifying")
            self.store_rpa_evidence(locked, rpa)
            claim_id = locked.claim_id
            run_id = str(rpa.get("run_id"))
            status = locked.status
        if result.outcome in {"submitted", "completed_existing"}:
            return self.service.reconcile.reconcile(
                projection_id, result=result, actor=RUNNER_ACTOR
            )
        if result.outcome == "ui_drift":
            return self._handle_ui_drift(
                projection_id, claim_id=claim_id, run_id=run_id, result=result
            )
        return self._fail(
            projection_id,
            claim_id=claim_id,
            run_id=run_id,
            subtype=(
                "uncertain_write"
                if result.outcome == "uncertain_write"
                else (
                    "edms_duplicate_filename_collision"
                    if result.reason_code == "edms_duplicate_filename_collision"
                    else "projection_failed"
                )
            ),
            reason_code=result.reason_code or result.outcome,
            result=result,
        ) | {"previous_status": status}

    def _handle_ui_drift(
        self, projection_id: str, *, claim_id: str, run_id: str, result: OpResult
    ) -> dict[str, Any]:
        row = self.service._row(projection_id)
        operation = self.operations.get(row.operation)
        version = operation.click_path.version if operation.click_path else "unknown"
        self.open_circuit(
            row.operation,
            definition_version=version,
            reason_code="ui_drift",
            actor=RUNNER_ACTOR,
        )
        self.service.create_exception(
            claim_id=claim_id,
            subtype="ui_drift",
            payload={
                "projection_id": projection_id,
                "operation": row.operation,
                "definition_version": version,
                "agent_run_id": run_id,
                "attempt": int(row.attempts or 0),
                "last_step": result.last_completed_step,
                "write_ids": list(result.write_ids),
                "evidence_ids": list(result.evidence_ids),
                "reason_code": result.reason_code,
            },
        )
        if result.may_have_written:
            # A write may have reached the target, so a human must establish
            # target state first. This row is never offered as paste-assist.
            return self._fail(
                projection_id,
                claim_id=claim_id,
                run_id=run_id,
                subtype="uncertain_write",
                reason_code="ui_drift_after_possible_write",
                result=result,
            )
        self.fall_back_to_paste(
            projection_id, reason_code="ui_drift", actor=RUNNER_ACTOR, run_id=run_id
        )
        return {"status": "queued", "mode": "paste_assist", "fallback": "paste_assist"}

    def _fail(
        self,
        projection_id: str,
        *,
        claim_id: str,
        run_id: str,
        subtype: str,
        reason_code: str,
        result: OpResult | None = None,
    ) -> dict[str, Any]:
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status in {"failed", "diverged"}:
                return {"status": locked.status, "replayed": True}
            self.service._transition(locked, "failed")
            locked.completed_at = self.clock()
            rpa = self.rpa_evidence(locked)
            rpa.pop("lease", None)
            rpa["terminal"] = {"subtype": subtype, "reason_code": reason_code}
            self.store_rpa_evidence(locked, rpa)
            attempt = int(locked.attempts or 0)
            self.service._emit(
                session,
                claim_id=claim_id,
                event_type="projection.failed",
                payload={
                    "projection_id": projection_id,
                    "operation": locked.operation,
                    "mode": locked.mode,
                    "reason": reason_code,
                    "subtype": subtype,
                    "attempt": attempt,
                    "agent_run_id": run_id,
                },
                actor=RUNNER_ACTOR,
            )
        self.service.create_exception(
            claim_id=claim_id,
            subtype=subtype,
            payload={
                "projection_id": projection_id,
                "operation": self.service._row(projection_id).operation,
                "agent_run_id": run_id,
                "attempt": attempt,
                "reason_code": reason_code,
                "last_step": None if result is None else result.last_completed_step,
                "write_ids": [] if result is None else list(result.write_ids),
                "evidence_ids": [] if result is None else list(result.evidence_ids),
            },
        )
        self.finish_run(
            run_id,
            status="failed",
            stages=("authorise", "lease", "execute"),
            outcome={"result": "failed", "reason_code": reason_code, "subtype": subtype},
            error={"code": subtype.upper(), "reason": reason_code},
        )
        return {"status": "failed", "subtype": subtype, "reason_code": reason_code}

    # -- crash recovery --------------------------------------------------------

    def reap_leases(self) -> dict[str, Any]:
        """Recover stale executing rows by exact readback, never by re-writing."""

        now = self.clock()
        with self.service.sessions() as session:
            rows = list(
                session.scalars(
                    select(Projection).where(
                        Projection.status == "executing", Projection.mode == "rpa"
                    )
                )
            )
            for row in rows:
                session.expunge(row)
        outcomes: list[dict[str, Any]] = []
        for row in rows:
            rpa = self.rpa_evidence(row)
            if self._live_lease(rpa, now) is not None:
                continue
            outcomes.append(self._recover(row))
        return {"scanned": len(rows), "recovered": outcomes}

    def _recover(self, row: Projection) -> dict[str, Any]:
        operation = self.operations.get(row.operation)
        click_path = operation.click_path
        rpa = self.rpa_evidence(row)
        run_id = str(rpa.get("run_id"))
        attempt = int(row.attempts or 0)
        record = self._attempt_record(rpa, attempt)
        write_ids = list(record.get("write_ids") or ())
        last_step = record.get("last_completed_step")
        possible_write_frame = any(
            isinstance(frame, dict)
            and frame.get("step_id") in {
                step.id for step in (click_path.write_steps if click_path is not None else ())
            }
            and frame.get("phase") in {"before", "after", "failure"}
            for frame in record.get("frames") or ()
        )
        wrote = bool(write_ids) or possible_write_frame or _write_reached(click_path, last_step)
        probe, observed = self._probe(row, operation)
        if probe == "exact_match":
            with self.service._guard(row.id) as session:
                locked = session.get(Projection, row.id)
                if locked is not None and locked.status == "executing":
                    self.service._transition(locked, "verifying")
                    current = self.rpa_evidence(locked)
                    current.pop("lease", None)
                    self.store_rpa_evidence(locked, current)
            result = OpResult(
                outcome="completed_existing",
                last_completed_step=last_step if isinstance(last_step, str) else None,
                write_ids=tuple(write_ids),
                # The probe is the only evidence of prior completion, so the
                # comparison uses exactly what it read back.
                readback_keys={
                    "inputs": dict(observed.get("record") or {}),
                    "outputs": dict(observed.get("outputs") or {}),
                },
                evidence_ids=tuple(
                    str(frame["evidence_id"])
                    for frame in record.get("frames") or ()
                    if isinstance(frame, dict) and isinstance(frame.get("evidence_id"), str)
                ),
                reason_code="recovered_prior_completion",
            )
            self.service.reconcile.reconcile(row.id, result=result, actor=RUNNER_ACTOR)
            return {"projection_id": row.id, "outcome": "recovered_prior_completion"}
        if probe == "absent" and not wrote:
            if attempt >= self.runtime.runner.max_attempts:
                self._fail(
                    row.id,
                    claim_id=row.claim_id,
                    run_id=run_id,
                    subtype="projection_attempts_exhausted",
                    reason_code="safe_attempts_exhausted",
                )
                return {"projection_id": row.id, "outcome": "attempts_exhausted"}
            with self.service._guard(row.id) as session:
                locked = session.get(Projection, row.id)
                if locked is not None and locked.status == "executing":
                    self.service._transition(locked, "queued")
                    current = self.rpa_evidence(locked)
                    current.pop("lease", None)
                    self.store_rpa_evidence(locked, current)
            return {"projection_id": row.id, "outcome": "requeued"}
        self._fail(
            row.id,
            claim_id=row.claim_id,
            run_id=run_id,
            subtype="uncertain_write",
            reason_code="probe_" + probe,
        )
        return {"projection_id": row.id, "outcome": "uncertain_write"}

    def _probe(
        self, row: Projection, operation: Operation
    ) -> tuple[str, dict[str, Any]]:
        """Ask the captured prior-completion probe. Never `Adapter.execute`."""

        click_path = operation.click_path
        adapter = self.adapters.get(operation.system)
        if click_path is None or adapter is None:
            return "unavailable", {}
        if click_path.is_read_only:
            # A read-only definition cannot have mutated the target.
            return "absent", {}
        probe = click_path.retry_probe
        if probe is None:
            return "unavailable", {}
        payload = row.payload if isinstance(row.payload, dict) else {}
        by_step = {
            str(entry.get("step_id")): entry
            for entry in payload.get("fields") or ()
            if isinstance(entry, dict)
        }
        keys: dict[str, str] = {}
        try:
            for key in probe.keys:
                entry = by_step[key.from_step]
                path = entry.get("path")
                value = entry.get("value")
                if isinstance(path, str) and not path.startswith(("rule:", "literal:")):
                    value = self.service.claims.reveal_snapshot_value(
                        row.claim_id, path=path, value=value, actor=RUNNER_ACTOR
                    )
                keys[key.target] = encode_copy_value(
                    value,
                    value_type=str(entry.get("value_type")),
                    encoding=str(entry.get("external_encoding")),
                )
        except (KeyError, ClaimCoreError, TypeError, ValueError):
            return "unavailable", {}
        try:
            found = adapter.readback(operation.id, keys)
        except AdapterUnavailable:
            return "unavailable", {}
        except Exception:  # noqa: BLE001 - an unreadable probe is never a completion
            return "unavailable", {}
        if not isinstance(found, dict):
            return "unavailable", {}
        matches = found.get("matches")
        record = found.get("record") or {}
        if matches == 1 and self.service.reconcile.snapshot_matches(row, record):
            return "exact_match", found
        if matches == 0:
            return "absent", found
        return "ambiguous", found

    # -- run lifecycle ---------------------------------------------------------

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stages: tuple[str, ...],
        outcome: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        """Close the deferred AR-1 run with the coarse stages actually reached."""

        try:
            self.app.state.agent_runtime.finish_deferred(
                run_id,
                status=status,
                outcome={
                    **outcome,
                    "stages": [
                        {"id": stage, "status": "completed", "ref": None}
                        for stage in stages
                        if stage in COP_STAGES
                    ],
                },
                error=error,
            )
        except LookupError:
            # The run was never created (a fixture may authorise directly); the
            # projection row remains the durable record either way.
            return

    def stage_state(self, projection_id: str, stage: str) -> dict[str, Any]:
        """Report one COP stage from durable evidence. Never a side effect."""

        row = self.service._row(projection_id)
        rpa = self.rpa_evidence(row)
        reached = {
            "authorise": rpa.get("authorisation") is not None or rpa.get("gate") is not None,
            "lease": bool(rpa.get("attempts")),
            "execute": any(
                isinstance(record, dict) and record.get("outcome") is not None
                for record in rpa.get("attempts") or ()
            ),
            "readback": row.readback is not None,
            "reconcile": row.status in {"completed", "failed", "diverged"},
        }
        if reached.get(stage):
            return {"stage": stage, "status": "reached"}
        return {"status": "waiting", "expects_event": "projection.completed"}


def _write_reached(click_path: Any, last_step: Any) -> bool:
    """Whether the durable last-completed step is at or past an external write."""

    if click_path is None or not isinstance(last_step, str):
        return False
    ordered = [step.id for step in click_path.steps]
    if last_step not in ordered:
        # An unrecognised step is not evidence of safety.
        return True
    return any(step.is_external_write for step in click_path.steps[: ordered.index(last_step) + 1])


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


__all__ = [
    "CIRCUIT_STATE_KEY",
    "COP_STAGES",
    "EVIDENCE_KEY",
    "LeaseGrant",
    "RPA_ACTION_TYPE",
    "RPA_SUBTYPE",
    "RUNNER_ACTOR",
    "RUNNER_STATE_KEY",
    "RpaCoordinator",
    "RunnerAuthenticator",
]
