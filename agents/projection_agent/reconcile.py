"""Canonical comparison and the divergence lifecycle (PACKET-21 §10).

Every successful RPA write enters `verifying`; it can never complete directly.
Reconciliation applies only the declared typed normalisers — there is no
implicit trimming, case folding, locale parsing, thousands separator, currency
prefix, timezone conversion, or rounding anywhere in this module. A target
representation nobody captured diverges rather than being guessed (register
#285).

Expected and actual values are protected with the claim DEK before they are
stored. Events, reviews, notifications, ledger rows, and ordinary APIs carry
only path, kind, hashes, and evidence ids (register #286).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from claim_core import ClaimCoreError, FieldWrite, field_dictionary
from claim_core.ledger import canonical_json
from projection_agent.adapters import OpResult
from projection_agent.models import Projection

DIVERGENCE_SCHEMA_VERSION = 1
DETECTORS = frozenset({"rpa_readback", "paste_sample", "nightly_drift"})
#: The four dispositions an `EXCEPTION{divergence}` may record. None of them
#: auto-corrects either side or reopens the old projection.
DISPOSITIONS = (
    "target_out_of_band",
    "platform_snapshot_wrong",
    "target_readback_wrong",
    "unresolved",
)
SHILLINGS = re.compile(r"^-?[0-9]+\.[0-9]{2}$")
CENTS = re.compile(r"^-?[0-9]+$")
#: Value kinds carried on a divergence path. `money` keeps its ED-8 meaning:
#: integer KES cents on both sides of the comparison.
KIND_BY_VALUE_TYPE = {
    "money": "money",
    "date": "date",
    "datetime": "date",
    "enum": "enum",
    "string": "text",
    "bool": "enum",
}


class NormalisationRefused(Exception):
    """The target representation is not the one the definition declares."""

    def __init__(self, normaliser: str, reason: str) -> None:
        self.normaliser = normaliser
        self.reason = reason
        super().__init__(f"{normaliser}: {reason}")


def normalise(kind: str, raw: Any) -> Any:
    """Turn one captured target representation into its canonical value."""

    if kind in {"string_exact", "enum_exact", "date_iso_exact", "datetime_iso_exact"}:
        if not isinstance(raw, str):
            raise NormalisationRefused(kind, "target value is not a string")
        return raw
    if kind == "money_cents_exact":
        if not isinstance(raw, str) or CENTS.fullmatch(raw) is None:
            raise NormalisationRefused(kind, "target value is not integer cents")
        return int(raw)
    if kind == "money_shillings_to_cents_exact":
        if not isinstance(raw, str) or SHILLINGS.fullmatch(raw) is None:
            raise NormalisationRefused(kind, "target value is not exact two-decimal shillings")
        try:
            # Exact decimal scaling. Two decimal places always scale to an
            # integer number of cents, so this never rounds.
            return int(Decimal(raw).scaleb(2))
        except (InvalidOperation, ValueError) as error:  # pragma: no cover - regex guards
            raise NormalisationRefused(kind, "target value is not a decimal") from error
    if kind == "bool_exact":
        if raw not in {"true", "false"}:
            raise NormalisationRefused(kind, "target value is not a literal boolean")
        return raw == "true"
    raise NormalisationRefused(kind, "normaliser is not declared")


def value_digest(value: Any) -> str:
    """A stable digest of one canonical value. Never the value itself."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Mismatch:
    """One reconciled path whose target representation is not the platform's."""

    path: str
    kind: str
    expected: Any
    actual: Any
    evidence_ids: tuple[str, ...] = ()


class ReconciliationEngine:
    """Compares one projection against its target and owns divergence."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.app = service.app
        self.operations = service.operations

    # -- shared helpers --------------------------------------------------------

    def _snapshot_inputs(self, row: Projection, actor: str) -> dict[str, dict[str, Any]]:
        """Decrypt the immutable snapshot inputs for this comparison only."""

        payload = row.payload if isinstance(row.payload, dict) else {}
        resolved: dict[str, dict[str, Any]] = {}
        for entry in payload.get("fields", ()):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            value = entry.get("value")
            if isinstance(path, str) and not path.startswith(("rule:", "literal:")):
                value = self.service.claims.reveal_snapshot_value(
                    row.claim_id, path=path, value=value, actor=actor
                )
            resolved[str(entry.get("step_id"))] = {**entry, "plain": value}
        return resolved

    @staticmethod
    def _kind(value_type: Any) -> str:
        return KIND_BY_VALUE_TYPE.get(str(value_type), "text")

    def _compare_inputs(
        self, row: Projection, observed: dict[str, Any], actor: str
    ) -> list[Mismatch]:
        operation = self.operations.get(row.operation)
        click_path = operation.click_path
        if click_path is None:
            raise ClaimCoreError(
                409,
                "PROJECTION_DEFINITION_UNAVAILABLE",
                "The operation definition is not currently live",
            )
        inputs = self._snapshot_inputs(row, actor)
        mismatches: list[Mismatch] = []
        for entry in click_path.reconciliation:
            snapshot = inputs.get(entry.step_id)
            if snapshot is None:
                continue
            raw = observed.get(entry.step_id)
            expected = snapshot["plain"]
            kind = self._kind(snapshot.get("value_type"))
            if raw is None:
                mismatches.append(
                    Mismatch(
                        path=str(snapshot.get("path")),
                        kind=kind,
                        expected=expected,
                        actual=None,
                    )
                )
                continue
            try:
                actual = normalise(entry.normaliser, raw)
            except NormalisationRefused:
                # An undeclared representation is a divergence, never a guess.
                mismatches.append(
                    Mismatch(
                        path=str(snapshot.get("path")),
                        kind=kind,
                        expected=expected,
                        actual=raw,
                    )
                )
                continue
            if actual != expected:
                mismatches.append(
                    Mismatch(
                        path=str(snapshot.get("path")),
                        kind=kind,
                        expected=expected,
                        actual=actual,
                    )
                )
        return mismatches

    def snapshot_matches(self, row: Projection, record: dict[str, Any]) -> bool:
        """Whether one probed target record equals the immutable snapshot exactly."""

        if not isinstance(record, dict) or not record:
            return False
        try:
            mismatches = self._compare_inputs(row, record, actor=self.service.rpa_actor)
        except ClaimCoreError:
            return False
        return not mismatches

    # -- immediate RPA reconciliation ------------------------------------------

    def reconcile(
        self, projection_id: str, *, result: OpResult, actor: str
    ) -> dict[str, Any]:
        """Verify one completed write, then complete exactly or diverge."""

        row = self.service._row(projection_id)
        if row.status == "completed":
            return {"status": "completed", "replayed": True}
        if row.status != "verifying":
            raise ClaimCoreError(
                409, "PROJECTION_STATE_STALE", "Reconciliation requires a verifying projection"
            )
        operation = self.operations.get(row.operation)
        click_path = operation.click_path
        if click_path is None or click_path.version != self.service.definition_version(row):
            return self._diverge(
                projection_id,
                detected_by="rpa_readback",
                mismatches=[],
                actor=actor,
                reason_code="definition_version_mismatch",
            )
        keys = result.readback_keys if isinstance(result.readback_keys, dict) else {}
        observed_inputs = keys.get("inputs")
        observed_outputs = keys.get("outputs")
        if not isinstance(observed_inputs, dict) or not isinstance(observed_outputs, dict):
            return self._diverge(
                projection_id,
                detected_by="rpa_readback",
                mismatches=[],
                actor=actor,
                reason_code="readback_shape_invalid",
            )
        if not self.service.rpa.evidence_complete(row, result):
            return self._diverge(
                projection_id,
                detected_by="rpa_readback",
                mismatches=[],
                actor=actor,
                reason_code="evidence_incomplete",
            )
        try:
            checked = self.service.paste.validate_readback(
                click_path, {entry.into: observed_outputs.get(entry.capture)
                             for entry in click_path.readback
                             if observed_outputs.get(entry.capture) is not None}
            )
        except ClaimCoreError:
            return self._diverge(
                projection_id,
                detected_by="rpa_readback",
                mismatches=[],
                actor=actor,
                reason_code="readback_format_invalid",
            )
        mismatches = self._compare_inputs(row, observed_inputs, actor)
        self._grade(row, mismatches, detected_by="rpa_readback", actor=actor)
        if mismatches:
            return self._diverge(
                projection_id,
                detected_by="rpa_readback",
                mismatches=mismatches,
                actor=actor,
                reason_code="input_mismatch",
                graded=True,
            )
        return self._complete(projection_id, checked=checked, result=result, actor=actor)

    def _complete(
        self,
        projection_id: str,
        *,
        checked: dict[str, str],
        result: OpResult,
        actor: str,
    ) -> dict[str, Any]:
        row = self.service._row(projection_id)
        operation = self.operations.get(row.operation)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by reconcile()
        source_ref = {
            "projection_id": row.id,
            "operation": row.operation,
            "operation_version": click_path.version,
            "attested_by": self.service.rpa_actor,
            "attested_at": self.service.clock().isoformat(),
        }
        writes: list[FieldWrite] = []
        for path, value in checked.items():
            existing = self.service.claims.snapshot_current_fields(row.claim_id, [path]).get(path)
            if existing is not None:
                reference = existing.source_ref if isinstance(existing.source_ref, dict) else {}
                if (
                    existing.verification_state == "human_verified"
                    or reference.get("projection_id") != row.id
                    or existing.value != value
                ):
                    self.service._readback_conflict(row, path)
                continue
            writes.append(
                FieldWrite(
                    path=path,
                    value=value,
                    value_type="string",
                    source_type="projection_readback",
                    source_ref=source_ref,
                    verification_state="system_confirmed",
                )
            )
        if writes:
            self.service.claims.write_fields(row.claim_id, writes, self.service.rpa_actor)
        run_id = None
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status == "completed":
                return {"status": "completed", "replayed": True}
            rpa = self.service.rpa.rpa_evidence(locked)
            run_id = rpa.get("run_id")
            self.service._transition(locked, "completed")
            locked.completed_at = self.service.clock()
            locked.readback = {
                "paths": sorted(checked),
                "values": {
                    path: self.service.claims.protect_snapshot_value(
                        locked.claim_id, path=path, value=value
                    )
                    for path, value in checked.items()
                },
            }
            self.service._emit(
                session,
                claim_id=locked.claim_id,
                event_type="projection.completed",
                payload={
                    "projection_id": locked.id,
                    "operation": locked.operation,
                    "mode": locked.mode,
                    "snapshot_hash": (locked.payload or {}).get("snapshot_hash"),
                    "readback_paths": sorted(checked),
                    "definition_version": click_path.version,
                    "agent_run_id": run_id,
                    "attempt": int(locked.attempts or 0),
                    "write_ids": list(result.write_ids),
                    "evidence_ids": [
                        frame["evidence_id"] for frame in self.service.rpa.frames(locked)
                    ],
                },
                actor=self.service.rpa_actor,
            )
            completed_row = locked
            completed = self.service._view(locked).as_dict()
        # PACKET-20 owns the canonical field/cache/FSM finalisation; reuse it so
        # `icon.claim_register` never appends its readback field twice.
        self.service._finish_claim_state(
            completed_row, dict(checked), actor=self.service.rpa_actor
        )
        if isinstance(run_id, str):
            self.service.rpa.finish_run(
                run_id,
                status="completed",
                stages=("authorise", "lease", "execute", "readback", "reconcile"),
                outcome={"result": "completed", "projection_id": projection_id},
            )
            # The coarse COP sequence is graded once the run is closed (§16).
            self.app.state.eval_harness.grade(
                "G-PROC",
                {
                    "claim_id": row.claim_id,
                    "agent_run_id": run_id,
                    "capability_id": f"project.{row.operation}",
                    "projection_id": projection_id,
                },
                actor,
            )
        return {"status": "completed", **completed}

    # -- divergence ------------------------------------------------------------

    def _grade(
        self, row: Projection, mismatches: list[Mismatch], *, detected_by: str, actor: str
    ) -> None:
        """Record one critical G-VAL reconciliation result for this projection.

        Standing drift is not autonomy-failure evidence until a human attributes
        it to the platform, so only immediate RPA and sampled-paste comparisons
        are graded here (§10).
        """

        if detected_by == "nightly_drift":
            return
        # The pass state is "no path diverged", so the two digests are equal
        # exactly when reconciliation matched.
        expected = value_digest([])
        actual = value_digest(sorted(mismatch.path for mismatch in mismatches))
        self.app.state.eval_harness.grade(
            "G-VAL",
            {
                "claim_id": row.claim_id,
                "path": f"projection:{row.operation}",
                "capability_id": f"project.{row.operation}",
                "projection_id": row.id,
                "reconciliation": {
                    "detected_by": detected_by,
                    "normaliser": "declared",
                    "mismatch_paths": sorted(mismatch.path for mismatch in mismatches),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                },
            },
            actor,
        )

    def _divergence_paths(
        self, row: Projection, mismatches: list[Mismatch]
    ) -> list[dict[str, Any]]:
        evidence_ids = [frame["evidence_id"] for frame in self.service.rpa.frames(row)]
        paths = []
        for mismatch in mismatches:
            protected_expected = self._protect(row, mismatch.path, mismatch.expected)
            protected_actual = self._protect(row, mismatch.path, mismatch.actual)
            paths.append(
                {
                    "path": mismatch.path,
                    "kind": mismatch.kind,
                    "expected": {"protected": protected_expected},
                    "actual": {"protected": protected_actual},
                    "expected_sha256": value_digest(mismatch.expected),
                    "actual_sha256": value_digest(mismatch.actual),
                    "evidence_ids": list(mismatch.evidence_ids or evidence_ids),
                }
            )
        return paths

    def _protect(self, row: Projection, path: str, value: Any) -> Any:
        if path not in field_dictionary():
            return value
        return self.service.claims.protect_snapshot_value(
            row.claim_id, path=path, value=value
        )

    def diverge(
        self,
        projection_id: str,
        *,
        detected_by: str,
        mismatches: list[Mismatch],
        actor: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Public entry for sampled-paste and standing-drift divergence."""

        return self._diverge(
            projection_id,
            detected_by=detected_by,
            mismatches=mismatches,
            actor=actor,
            reason_code=reason_code,
        )

    def _diverge(
        self,
        projection_id: str,
        *,
        detected_by: str,
        mismatches: list[Mismatch],
        actor: str,
        reason_code: str,
        graded: bool = False,
    ) -> dict[str, Any]:
        if detected_by not in DETECTORS:
            raise ValueError(f"unknown divergence detector {detected_by!r}")
        row = self.service._row(projection_id)
        if row.status == "diverged":
            # Exact repeat creates no duplicate event or review.
            return {"status": "diverged", "replayed": True}
        if not graded:
            self._grade(row, mismatches, detected_by=detected_by, actor=actor)
        paths = self._divergence_paths(row, mismatches)
        detected_at = self.service.clock()
        run_id: Any = None
        with self.service._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status == "diverged":
                return {"status": "diverged", "replayed": True}
            rpa = self.service.rpa.rpa_evidence(locked)
            run_id = rpa.get("run_id")
            self.service._transition(locked, "diverged")
            locked.completed_at = detected_at
            locked.divergence = {
                "schema_version": DIVERGENCE_SCHEMA_VERSION,
                "detected_by": detected_by,
                "detected_at": detected_at.isoformat(),
                "reason_code": reason_code,
                "paths": paths,
            }
            self.service._emit(
                session,
                claim_id=locked.claim_id,
                event_type="projection.diverged",
                payload={
                    "projection_id": locked.id,
                    "operation": locked.operation,
                    "mode": locked.mode,
                    "detected_by": detected_by,
                    "reason_code": reason_code,
                    "agent_run_id": run_id,
                    "attempt": int(locked.attempts or 0),
                    # Paths, kinds, hashes, and evidence ids only.
                    "paths": [
                        {
                            "path": entry["path"],
                            "kind": entry["kind"],
                            "expected_sha256": entry["expected_sha256"],
                            "actual_sha256": entry["actual_sha256"],
                            "evidence_ids": entry["evidence_ids"],
                        }
                        for entry in paths
                    ],
                },
                actor=actor,
            )
            claim_id = locked.claim_id
            operation = locked.operation
        self.service.create_exception(
            claim_id=claim_id,
            subtype="divergence",
            payload={
                "projection_id": projection_id,
                "operation": operation,
                "detected_by": detected_by,
                "reason_code": reason_code,
                "agent_run_id": run_id,
                "dispositions": list(DISPOSITIONS),
                "paths": [
                    {
                        "path": entry["path"],
                        "kind": entry["kind"],
                        "expected_sha256": entry["expected_sha256"],
                        "actual_sha256": entry["actual_sha256"],
                        "evidence_ids": entry["evidence_ids"],
                    }
                    for entry in paths
                ],
            },
        )
        if isinstance(run_id, str):
            self.service.rpa.finish_run(
                run_id,
                status="failed",
                stages=("authorise", "lease", "execute", "readback", "reconcile"),
                outcome={"result": "diverged", "reason_code": reason_code},
                error={"code": "PROJECTION_DIVERGED", "reason": reason_code},
            )
        return {"status": "diverged", "reason_code": reason_code, "paths": len(paths)}

    # -- comparison for sampled paste and standing drift -----------------------

    def compare_observed(
        self, row: Projection, observed: dict[str, Any], *, actor: str
    ) -> list[Mismatch]:
        """Compare captured target values against the immutable snapshot/readback."""

        # A sampled paste capture supplies only the declared target *outputs*;
        # comparing absent inputs would invent fourteen mismatches.
        inputs = observed.get("inputs") or {}
        mismatches = self._compare_inputs(row, inputs, actor) if inputs else []
        stored = row.readback if isinstance(row.readback, dict) else {}
        values = stored.get("values") if isinstance(stored.get("values"), dict) else {}
        for path, protected in values.items():
            if path not in observed.get("outputs", {}):
                continue
            expected = self.service.claims.reveal_snapshot_value(
                row.claim_id, path=path, value=protected, actor=actor
            )
            actual = observed["outputs"][path]
            if actual != expected:
                mismatches.append(
                    Mismatch(path=path, kind="text", expected=expected, actual=actual)
                )
        return mismatches


__all__ = [
    "DISPOSITIONS",
    "DIVERGENCE_SCHEMA_VERSION",
    "Mismatch",
    "NormalisationRefused",
    "ReconciliationEngine",
    "normalise",
    "value_digest",
]
