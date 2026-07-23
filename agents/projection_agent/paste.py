"""Click-path-derived paste strip, snapshot, and readback mechanics.

Nothing in this module talks to a target system. It turns one validated click
path plus durable claim data into an immutable payload snapshot and, for an
authorised reader, into copy rows whose clipboard text is exactly the server's
``copy_value``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from claim_core import ClaimCoreError, field_dictionary
from claim_core.ledger import canonical_json
from projection_agent.config import ClickPath, Operation, Step

PAYLOAD_SCHEMA_VERSION = 1
#: Register #274: PRD-09 does not state a verification floor for a value an
#: officer is told to type into ICON, so paste-assist takes the narrowest safe
#: one — the platform must have confirmed the value itself or a human must have
#: verified it. A merely `extracted` value blocks rather than being copied.
VERIFICATION_RANK = {"extracted": 0, "system_confirmed": 1, "human_verified": 2}
MIN_VERIFICATION = "system_confirmed"


class SnapshotBlocked(Exception):
    """One declared input could not be resolved from durable sources."""

    def __init__(self, blocked_on: str) -> None:
        super().__init__(blocked_on)
        self.blocked_on = blocked_on


@dataclass(frozen=True)
class Snapshot:
    """The immutable payload written into ``projections.payload``."""

    payload: dict[str, Any]
    snapshot_hash: str

    def idempotency_key(self, claim_id: str, operation: str) -> str:
        return f"{claim_id}:{operation}:{self.snapshot_hash}"


def encode_copy_value(value: Any, *, value_type: str, encoding: str) -> str:
    """Return the exact string the clipboard receives. The browser adds nothing."""

    if encoding == "cents":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SnapshotBlocked("money_value_not_integer_cents")
        return str(value)
    if encoding == "shillings":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SnapshotBlocked("money_value_not_integer_cents")
        # Exact decimal division by 100. Cents always carry at most two decimal
        # places, so this quantisation never rounds; no commas, no prefix.
        return str((Decimal(value) / Decimal(100)).quantize(Decimal("0.01")))
    if encoding == "iso":
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str) or not value:
            raise SnapshotBlocked("date_value_not_iso")
        return value
    if encoding == "raw":
        if isinstance(value, str):
            return value
        raise SnapshotBlocked(f"value_type_{value_type}_has_no_captured_encoding")
    raise SnapshotBlocked(f"unknown_encoding_{encoding}")


class PasteEngine:
    """Builds snapshots and paste strips for one application."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.claims = app.state.claim_service

    # -- snapshot --------------------------------------------------------------

    def _rule_binding(self, claim_id: str, step: Step) -> dict[str, Any]:
        """Resolve one rule binding to a single completed run and its version."""

        with self.app.state.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, rule_version, fired, evaluated_at FROM rule_runs "
                        "WHERE claim_id = :claim_id AND rule_id = :rule_id "
                        "AND status = 'evaluated' ORDER BY evaluated_at DESC, id DESC"
                    ),
                    {"claim_id": claim_id, "rule_id": step.rule_id},
                )
                .mappings()
                .all()
            )
        if not rows:
            raise SnapshotBlocked(f"rule_run_missing:{step.rule_id}")
        latest = rows[0]
        contemporaries = [row for row in rows if row["evaluated_at"] == latest["evaluated_at"]]
        if len(contemporaries) > 1:
            raise SnapshotBlocked(f"rule_run_ambiguous:{step.rule_id}")
        fired = latest["fired"]
        if fired is None:
            raise SnapshotBlocked(f"rule_run_indeterminate:{step.rule_id}")
        assert step.rule_values is not None  # guaranteed by the click-path loader
        return {
            "step_id": step.id,
            "path": f"rule:{step.rule_id}",
            "field_id": str(latest["id"]),
            "version": str(latest["rule_version"]),
            "value_type": "string",
            "verification_state": "system_confirmed",
            "value": step.rule_values["true" if bool(fired) else "false"],
            "external_encoding": step.external_encoding or "raw",
        }

    def build_snapshot(
        self, claim_id: str, operation: Operation, *, source_event_id: str | None
    ) -> Snapshot:
        """Snapshot every declared input, or block without persisting anything."""

        click_path = operation.click_path
        if click_path is None:
            raise SnapshotBlocked(operation.blocked_on or "operation_not_live")
        copy_steps = [step for step in click_path.steps if step.is_copy_row]
        field_paths = [
            step.field_path for step in copy_steps if step.value_kind == "field"
        ]
        snapshots = self.claims.snapshot_current_fields(claim_id, field_paths)
        dictionary = field_dictionary()
        fields: list[dict[str, Any]] = []
        for step in copy_steps:
            if step.value_kind == "rule":
                fields.append(self._rule_binding(claim_id, step))
                continue
            if step.value_kind == "literal":
                fields.append(
                    {
                        "step_id": step.id,
                        "path": f"literal:{step.id}",
                        "field_id": None,
                        "version": click_path.version,
                        "value_type": "string",
                        "verification_state": "system_confirmed",
                        "value": step.literal,
                        "external_encoding": step.external_encoding or "raw",
                    }
                )
                continue
            path = step.field_path
            assert path is not None  # guaranteed by the click-path loader
            current = snapshots.get(path)
            if current is None:
                raise SnapshotBlocked(f"field_missing:{path}")
            if VERIFICATION_RANK.get(current.verification_state, -1) < VERIFICATION_RANK[
                MIN_VERIFICATION
            ]:
                raise SnapshotBlocked(f"field_under_verified:{path}")
            if step.external_encoding is None:
                raise SnapshotBlocked(f"target_encoding_undeclared:{step.id}")
            if dictionary[path].value_type == "money" and (
                not isinstance(current.value, int) or isinstance(current.value, bool)
            ):
                raise SnapshotBlocked(f"money_not_integer_cents:{path}")
            fields.append(
                {
                    "step_id": step.id,
                    "path": path,
                    "field_id": current.field_id,
                    "version": current.version,
                    "value_type": current.value_type,
                    "verification_state": current.verification_state,
                    # The exact stored form: an AES-256-GCM envelope stays an
                    # envelope, so `payload` never becomes a plaintext PII store
                    # and the hash stays stable for one field version (#266/#267).
                    "value": current.value,
                    "external_encoding": step.external_encoding,
                }
            )
        material = {
            "operation_definition": {
                "operation": operation.id,
                "version": click_path.version,
            },
            "fields": fields,
        }
        snapshot_hash = hashlib.sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            **material,
            "source_event_id": source_event_id,
            "snapshot_hash": snapshot_hash,
        }
        return Snapshot(payload=payload, snapshot_hash=snapshot_hash)

    # -- paste strip -----------------------------------------------------------

    def groups(
        self,
        *,
        claim_id: str,
        payload: dict[str, Any],
        click_path: ClickPath,
        evidence: dict[str, Any],
        actor: str,
    ) -> list[dict[str, Any]]:
        """Render the ordered strip, decrypting only for this authorised view."""

        by_step = {entry["step_id"]: entry for entry in payload.get("fields", [])}
        done_map = evidence.get("groups", {}) if isinstance(evidence, dict) else {}
        rendered: list[dict[str, Any]] = []
        for screen in click_path.screens:
            rows: list[dict[str, Any]] = []
            for step in click_path.steps_for(screen.id):
                entry = by_step.get(step.id)
                if entry is None or not step.is_copy_row:
                    continue
                value = entry["value"]
                if isinstance(entry["path"], str) and entry["path"].startswith(
                    ("rule:", "literal:")
                ):
                    plain = value
                else:
                    plain = self.claims.reveal_snapshot_value(
                        claim_id, path=entry["path"], value=value, actor=actor
                    )
                rows.append(
                    {
                        "step_id": step.id,
                        "label": step.label,
                        "path": entry["path"],
                        "copy_value": encode_copy_value(
                            plain,
                            value_type=entry["value_type"],
                            encoding=entry["external_encoding"],
                        ),
                        "external_encoding": entry["external_encoding"],
                        "value_type": entry["value_type"],
                        "field_version": entry["version"],
                    }
                )
            state = done_map.get(screen.id) if isinstance(done_map, dict) else None
            rendered.append(
                {
                    "id": screen.id,
                    "label": screen.label,
                    "done": isinstance(state, dict) and state.get("done") is True,
                    "fields": rows,
                }
            )
        return rendered

    # -- readback --------------------------------------------------------------

    @staticmethod
    def readback_fields(click_path: ClickPath) -> list[dict[str, Any]]:
        rows = []
        for entry in click_path.readback:
            validator = click_path.validators[entry.assert_format]
            rows.append(
                {
                    "label": entry.label,
                    "path": entry.into,
                    "required": entry.required,
                    "format_status": validator.status,
                    "blocked_on": validator.blocked_on,
                }
            )
        return rows

    @staticmethod
    def validate_readback(
        click_path: ClickPath, submitted: dict[str, Any]
    ) -> dict[str, str]:
        """Accept exactly the declared keys, each matching its captured format."""

        declared = {entry.into: entry for entry in click_path.readback}
        unknown = sorted(set(submitted) - set(declared))
        if unknown:
            raise ClaimCoreError(
                422,
                "READBACK_KEY_UNKNOWN",
                f"Readback keys {unknown} are not declared by this operation",
            )
        missing = sorted(
            path
            for path, entry in declared.items()
            if entry.required and path not in submitted
        )
        if missing:
            raise ClaimCoreError(
                422,
                "READBACK_REQUIRED",
                f"Readback values {missing} are required by this operation",
            )
        checked: dict[str, str] = {}
        for path, value in submitted.items():
            entry = declared[path]
            validator = click_path.validators[entry.assert_format]
            if validator.status != "live":
                raise ClaimCoreError(
                    422,
                    "READBACK_FORMAT_PENDING_CAPTURE",
                    f"The format for {path!r} is not captured ({validator.blocked_on})",
                )
            if not isinstance(value, str) or not value.strip():
                raise ClaimCoreError(
                    422, "READBACK_MALFORMED", f"Readback {path!r} must be a non-empty string"
                )
            assert validator.pattern is not None  # guaranteed by the loader
            if validator.pattern.fullmatch(value) is None:
                raise ClaimCoreError(
                    422,
                    "READBACK_FORMAT_INVALID",
                    f"Readback {path!r} does not match {entry.assert_format}",
                )
            checked[path] = value
        return checked


__all__ = [
    "MIN_VERIFICATION",
    "PAYLOAD_SCHEMA_VERSION",
    "PasteEngine",
    "Snapshot",
    "SnapshotBlocked",
    "encode_copy_value",
]
