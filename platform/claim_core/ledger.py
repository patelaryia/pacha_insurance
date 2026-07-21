"""Single-writer audit ledger, verification, and immutable anchoring."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from threading import Lock
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from claim_core.models import AuditLedgerRow, Event, PlatformState
from claim_core.service import new_ulid, utc_now
from claim_core.storage import BlobStore

ACTION_MAP = {
    "field.updated": "field.version",
    "field.verified": "field.verified",
    "claim.status_changed": "fsm.transition",
    "review.created": "review.action",
    "review.resolved": "review.resolved",
    "authz.denied": "authz.denied",
    "claim.created": "claim.created",
    "claim.assigned": "claim.assigned",
    "document.received": "document.received",
    "document.extracted": "document.extracted",
    "document.rejected": "document.rejected",
    "model.called": "model.structured_call",
    "pii.decrypted": "pii.decrypt",
    "autonomy.promoted": "autonomy.promoted",
    "autonomy.demoted": "autonomy.demoted",
    "notify.sent": "notify.sent",
    "notify.staged": "notify.staged",
    "sla.escalated": "sla.escalated",
    "chase.init": "chase.init",
    "chase.item_requested": "chase.item_requested",
    "chase.item_received": "chase.item_received",
    "chase.item_verified": "chase.item_verified",
    "chase.item_rejected": "chase.item_rejected",
    "chase.item_waived": "chase.item_waived",
    "chase.item_snoozed": "chase.item_snoozed",
    "chase.reminder_sent": "chase.reminder_sent",
    "chase.complete": "chase.complete",
    "chase.cancelled": "chase.cancelled",
    "fraud.signal": "fraud.signal",
    "assessment.mode_item_created": "assessment.mode_item_created",
    "assessment.mode_decided": "assessment.mode_decided",
    "assessment.dispatched": "assessment.dispatched",
    "assessment.report_received": "assessment.report_received",
    "assessment.selection_completed": "assessment.selection_completed",
    "projection.requested": "projection.requested",
    "savings.recorded": "savings.recorded",
}

EventRecorder = Callable[..., Event]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return binding sorted, compact UTF-8 JSON text."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _row_material(row: AuditLedgerRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {
        "id": row.id,
        "seq": row.seq,
        "occurred_at": row.occurred_at,
        "actor": row.actor,
        "action": row.action,
        "claim_id": row.claim_id,
        "object_ref": row.object_ref,
        "before_hash": row.before_hash,
        "after_hash": row.after_hash,
        "detail": row.detail,
    }


class LedgerWriter:
    """The sole code path authorised to insert audit-ledger rows."""

    def __init__(
        self,
        session_factory: sessionmaker,
        blob_store: BlobStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        event_recorder: EventRecorder | None = None,
    ) -> None:
        self._sessions = session_factory
        self._blob_store = blob_store
        self._clock = clock
        self._event = event_recorder
        self._lock = Lock()

    @staticmethod
    def _database_lock(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('audit_ledger_writer'))")
            )

    @staticmethod
    def _event_already_written(session: Session, event_id: str) -> bool:
        row_id = session.scalar(
            select(AuditLedgerRow.id)
            .where(AuditLedgerRow.detail["event_id"].as_string() == event_id)
            .limit(1)
        )
        return row_id is not None

    def _append(
        self,
        *,
        occurred_at: datetime,
        actor: str,
        action: str,
        claim_id: str | None,
        object_ref: str | None,
        before_hash: str | None,
        after_hash: str | None,
        detail: dict[str, Any],
        event_id: str | None = None,
    ) -> AuditLedgerRow | None:
        with self._lock:
            with self._sessions.begin() as session:
                self._database_lock(session)
                if event_id is not None and self._event_already_written(session, event_id):
                    return None
                previous = session.scalar(
                    select(AuditLedgerRow).order_by(AuditLedgerRow.seq.desc()).limit(1)
                )
                seq = 1 if previous is None else previous.seq + 1
                material = {
                    "id": new_ulid(),
                    "seq": seq,
                    "occurred_at": occurred_at,
                    "actor": actor,
                    "action": action,
                    "claim_id": claim_id,
                    "object_ref": object_ref,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "detail": detail,
                }
                previous_hash = "" if previous is None else previous.row_hash
                row = AuditLedgerRow(
                    **material,
                    row_hash=hashlib.sha256(
                        (previous_hash + canonical_json(material)).encode("utf-8")
                    ).hexdigest(),
                )
                session.add(row)
                session.flush()
                session.expunge(row)
                return row

    def consume(self, event: Event) -> None:
        """Map one domain event to at most one idempotent ledger row."""

        action = ACTION_MAP.get(event.type)
        if action is None:
            return
        payload = event.payload
        object_ref = (
            payload.get("field_id")
            or payload.get("document_id")
            or payload.get("review_id")
            or payload.get("task")
            or payload.get("field_path")
            or event.claim_id
        )
        before_hash = (
            _sha256_json(payload["before"]) if "before" in payload else None
        )
        after_hash = _sha256_json(payload["after"]) if "after" in payload else None
        self._append(
            occurred_at=event.occurred_at,
            actor=event.actor,
            action=action,
            claim_id=event.claim_id,
            object_ref=object_ref,
            before_hash=before_hash,
            after_hash=after_hash,
            detail={"event_id": event.id, "event_type": event.type, "payload": payload},
            event_id=event.id,
        )

    def verify_chain(self) -> dict[str, bool | int | None]:
        """Recompute the complete chain and identify the first tampered row."""

        with self._sessions() as session:
            rows = list(
                session.scalars(select(AuditLedgerRow).order_by(AuditLedgerRow.seq))
            )
        previous_hash = ""
        for index, row in enumerate(rows, start=1):
            expected = hashlib.sha256(
                (previous_hash + canonical_json(_row_material(row))).encode("utf-8")
            ).hexdigest()
            if row.seq != index or row.row_hash != expected:
                return {"ok": False, "checked": index, "first_bad_seq": row.seq}
            previous_hash = row.row_hash
        return {"ok": True, "checked": len(rows), "first_bad_seq": None}

    def anchor_head(self) -> dict[str, str | int]:
        """Write today's ledger head to the configured immutable-store boundary."""

        with self._sessions() as session:
            head = session.scalar(
                select(AuditLedgerRow).order_by(AuditLedgerRow.seq.desc()).limit(1)
            )
        anchor = {
            "date": self._clock().date().isoformat(),
            "head_seq": 0 if head is None else head.seq,
            "head_hash": "" if head is None else head.row_hash,
        }
        self._blob_store.put(
            f"audit-anchors/{anchor['date']}.json",
            canonical_json(anchor).encode("utf-8"),
        )
        return anchor

    @staticmethod
    def _set_state(session: Session, key: str, value: Any, now: datetime) -> None:
        row = session.get(PlatformState, key)
        if row is None:
            session.add(PlatformState(key=key, value=value, updated_at=now))
        else:
            row.value = value
            row.updated_at = now

    def run_nightly_verification(self) -> dict[str, bool | int | None]:
        """Verify, anchor when healthy, and enter audit-degraded mode on failure."""

        report = self.verify_chain()
        if report["ok"]:
            self.anchor_head()
            return report
        now = self._clock()
        with self._sessions.begin() as session:
            self._set_state(session, "audit_degraded", True, now)
            self._set_state(session, "autonomy_promotions_frozen", True, now)
            if self._event is not None:
                self._event(
                    session,
                    claim_id=None,
                    event_type="ops.alert",
                    payload={
                        "subtype": "audit_chain_verification_failed",
                        "first_bad_seq": report["first_bad_seq"],
                    },
                    actor="system",
                    correlation_id=new_ulid(),
                )
        return report

    def dual_write_required(self) -> bool:
        """Expose the audit-degraded hook used by future L3+ actions."""

        with self._sessions() as session:
            value = session.scalar(
                select(PlatformState.value).where(
                    PlatformState.key == "audit_degraded"
                )
            )
        return value is True
