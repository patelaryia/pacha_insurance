"""Standing read-only drift reconciliation (PACKET-21 §14, register #290).

The task, the registry, and the schedule slot exist. The production values do
not: the nightly EAT time, the ICON claim-status value map, and the target
readback selectors are absent from the source documents, so `drift.yaml` ships
`pending_capture` and Beat registration stays visibly disabled until PACKET-22
captures them. A fixture registry proves the within-24-hour mechanic.

Drift never writes to a target and never re-runs an operation. It reuses the
same typed normalisers and the same divergence lifecycle as immediate
reconciliation, and a completed projection may become diverged — it never
silently returns to completed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from projection_agent.adapters import AdapterUnavailable
from projection_agent.config import DriftCheck
from projection_agent.models import Projection
from projection_agent.reconcile import Mismatch, NormalisationRefused, normalise

DRIFT_ACTOR = "agent:projection-drift"


class DriftEngine:
    """Runs the registered nightly checks against completed RPA projections."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.app = service.app
        self.operations = service.operations

    @property
    def config(self) -> Any:
        return self.operations.drift

    def status(self) -> dict[str, Any]:
        """The honest registry state, for Systems and the runbook."""

        return {
            "status": self.config.status,
            "blocked_on": self.config.blocked_on,
            "schedulable": self.config.schedulable,
            "checks": [
                {
                    "id": check.id,
                    "status": check.status,
                    "blocked_on": check.blocked_on,
                    "source_operation": check.source_operation,
                    "claim_path": check.claim_path,
                }
                for check in self.config.checks
            ],
        }

    def run(self, *, actor: str = DRIFT_ACTOR) -> dict[str, Any]:
        """One idempotent cycle. A pending registry does nothing, visibly."""

        if self.config.status != "live":
            return {
                "status": "blocked_on_inputs",
                "blocked_on": self.config.blocked_on,
                "checked": 0,
                "diverged": 0,
            }
        checked = 0
        diverged = 0
        for check in self.config.live_checks:
            for row in self._candidates(check):
                checked += 1
                if self._check_one(row, check, actor=actor):
                    diverged += 1
        return {"status": "ran", "checked": checked, "diverged": diverged}

    def _candidates(self, check: DriftCheck) -> list[Projection]:
        """Only completed RPA rows for this operation with no open divergence."""

        with self.service.sessions() as session:
            rows = list(
                session.scalars(
                    select(Projection)
                    .where(
                        Projection.status == "completed",
                        Projection.mode == "rpa",
                        Projection.operation == check.source_operation,
                    )
                    .order_by(Projection.created_at, Projection.id)
                )
            )
            for row in rows:
                session.expunge(row)
        operation = self.operations.get(check.source_operation)
        version = operation.click_path.version if operation.click_path else None
        eligible = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            definition = payload.get("operation_definition") or {}
            if version is None or definition.get("version") != version:
                continue
            reference = self.service.claims.snapshot_current_fields(
                row.claim_id, [check.external_ref]
            ).get(check.external_ref)
            if reference is None:
                continue
            eligible.append(row)
        return eligible

    def _check_one(self, row: Projection, check: DriftCheck, *, actor: str) -> bool:
        operation = self.operations.get(check.source_operation)
        adapter = self.service.rpa.adapters.get(operation.system)
        click_path = operation.click_path
        if adapter is None or click_path is None or check.claim_path is None:
            return False
        try:
            observed = adapter.readback(
                operation.id,
                {"drift_check": check.id, "target_readback": check.target_readback},
            )
        except AdapterUnavailable:
            return False
        if not isinstance(observed, dict):
            return False
        raw = observed.get("value")
        entry = next(
            (
                item
                for item in click_path.reconciliation
                if click_path.step(item.step_id).field_path == check.claim_path
            ),
            None,
        )
        if entry is None or raw is None:
            return False
        snapshot = self.service.claims.snapshot_current_fields(
            row.claim_id, [check.claim_path]
        ).get(check.claim_path)
        if snapshot is None:
            return False
        expected = self.service.claims.reveal_snapshot_value(
            row.claim_id, path=check.claim_path, value=snapshot.value, actor=actor
        )
        try:
            actual = normalise(entry.normaliser, raw)
        except NormalisationRefused:
            actual = raw
        if actual == expected:
            return False
        self.service.reconcile.diverge(
            row.id,
            detected_by="nightly_drift",
            mismatches=[
                Mismatch(
                    path=check.claim_path,
                    kind="money" if snapshot.value_type == "money" else "text",
                    expected=expected,
                    actual=actual,
                )
            ],
            actor=actor,
            reason_code=f"drift_{check.id}",
        )
        return True


__all__ = ["DRIFT_ACTOR", "DriftEngine"]
