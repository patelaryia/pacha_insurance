"""Projection request, snapshot, idempotency, and paste-assist lifecycle.

Nothing here writes to a target system. Paste-assist is authenticated human
work, so it deliberately does not pass through ``execute_or_stage``: PACKET-21
registers the first external executor behind the AR-2 gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from claim_core import ClaimCoreError, FieldWrite, new_ulid
from claim_core.ledger import canonical_json
from projection_agent.config import Operation, OperationRegistry
from projection_agent.models import LEGAL_EDGES, Projection
from projection_agent.paste import PasteEngine, SnapshotBlocked

ACTOR = "agent:projection"
#: PRD-04 operational roles may read the Systems surface; `auditor` is read-only.
READ_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "gm",
        "md",
        "chairman",
        "auditor",
    }
)
#: Only these three roles may drive the strip (§7).
OPERATE_ROLES = frozenset({"claims_officer", "asst_claims_manager", "claims_manager"})
#: The exact earlier PACKET-17 reserve request payload (register #268).
LEGACY_RESERVE_KEYS = frozenset({"claim_id", "calc_run_id", "reserve_total"})
LEGACY_RESERVE_OPERATION = "icon.reserve_create"
LOCK_STRIPES = 64


@dataclass(frozen=True)
class ProjectionResult:
    """The outcome of one projection request. A blocked request creates no row."""

    status: str
    operation: str
    projection_id: str | None = None
    blocked_on: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "projection_id": self.projection_id,
            "blocked_on": self.blocked_on,
        }


@dataclass(frozen=True)
class ProjectionView:
    """A PII-safe projection summary. Values never appear here."""

    id: str
    claim_id: str
    operation: str
    capability_id: str
    mode: str
    status: str
    snapshot_hash: str | None
    definition_version: str | None
    blocked_on: str | None
    readback_paths: tuple[str, ...] = ()
    attested_by: str | None = None
    attested_at: str | None = None
    paste_seconds: int | None = None
    started_at: str | None = None
    groups_done: dict[str, bool] = field(default_factory=dict)
    created_at: str | None = None
    completed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim_id": self.claim_id,
            "operation": self.operation,
            "capability_id": self.capability_id,
            "mode": self.mode,
            "status": self.status,
            "snapshot_hash": self.snapshot_hash,
            "definition_version": self.definition_version,
            "blocked_on": self.blocked_on,
            "readback_paths": list(self.readback_paths),
            "attested_by": self.attested_by,
            "attested_at": self.attested_at,
            "paste_seconds": self.paste_seconds,
            "started_at": self.started_at,
            "groups_done": dict(self.groups_done),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


def _iso(value: datetime | None) -> str | None:
    """Render one stored timestamp as UTC. SQLite hands back naive datetimes."""

    if value is None:
        return None
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).isoformat()


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return value
    return value


class ProjectionService:
    """The curated service exposed as ``app.state.projection_agent``."""

    def __init__(self, app: Any, operations: OperationRegistry) -> None:
        self.app = app
        self.operations = operations
        self.claims = app.state.claim_service
        self.clock = app.state.clock
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.paste = PasteEngine(app)
        self._locks = tuple(RLock() for _ in range(LOCK_STRIPES))

    # -- shared helpers --------------------------------------------------------

    def _role(self, actor: str) -> str:
        role = self.app.state.review_queue.service.authorizer.role(actor)
        if role is None:
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Actor has no configured role")
        return str(role)

    def require_read_role(self, actor: str) -> str:
        role = self._role(actor)
        if role not in READ_ROLES:
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Role cannot read projections")
        return role

    def require_operate_role(self, actor: str) -> str:
        role = self._role(actor)
        if role not in OPERATE_ROLES:
            raise ClaimCoreError(
                403, "FORBIDDEN_ROLE", "Role cannot operate the paste strip"
            )
        return role

    @contextmanager
    def _guard(self, projection_id: str) -> Iterator[Session]:
        """Serialise status changes: a row lock on PostgreSQL, a local lock otherwise."""

        stripe = self._locks[hash(projection_id) % LOCK_STRIPES]
        with stripe:
            with self.sessions.begin() as session:
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT id FROM projections WHERE id = :id FOR UPDATE"),
                        {"id": projection_id},
                    )
                yield session

    def _emit(
        self,
        session: Session,
        *,
        claim_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        correlation_id: str | None = None,
    ) -> None:
        self.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
            correlation_id=correlation_id or new_ulid(),
        )

    def _events(self, claim_id: str | None, event_type: str) -> list[dict[str, Any]]:
        statement = "SELECT id, claim_id, payload FROM events WHERE type = :type"
        parameters: dict[str, Any] = {"type": event_type}
        if claim_id is not None:
            statement += " AND claim_id = :claim_id"
            parameters["claim_id"] = claim_id
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(text(statement + " ORDER BY seq"), parameters).mappings()
            return [{**dict(row), "payload": _json(row["payload"])} for row in rows]

    @staticmethod
    def _evidence(row: Projection) -> dict[str, Any]:
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        paste = evidence.get("paste_assist")
        return dict(paste) if isinstance(paste, dict) else {}

    def _store_evidence(self, row: Projection, paste: dict[str, Any]) -> None:
        evidence = dict(row.evidence) if isinstance(row.evidence, dict) else {}
        evidence["paste_assist"] = paste
        row.evidence = evidence

    @staticmethod
    def _transition(row: Projection, to: str) -> None:
        if (row.status, to) not in LEGAL_EDGES:
            raise ClaimCoreError(
                409,
                "PROJECTION_STATE_STALE",
                f"Projection cannot move from {row.status} to {to}",
            )
        row.status = to

    # -- structural validity ---------------------------------------------------

    def _definition(self, row: Projection) -> Operation:
        """Return the operation a persisted row names, failing closed if unusable."""

        payload = row.payload if isinstance(row.payload, dict) else {}
        definition = payload.get("operation_definition")
        if (
            payload.get("schema_version") != 1
            or not isinstance(definition, dict)
            or definition.get("operation") != row.operation
            or not isinstance(payload.get("fields"), list)
            or row.operation not in self.operations
        ):
            self._fail_closed(row.id, "payload_structurally_invalid")
        operation = self.operations.get(row.operation)
        if operation.click_path is None or operation.click_path.version != definition.get(
            "version"
        ):
            # A configuration that is merely pending capture is not a runtime
            # failure; the row simply has no executable definition right now.
            raise ClaimCoreError(
                409,
                "PROJECTION_DEFINITION_UNAVAILABLE",
                f"Operation {row.operation} version "
                f"{definition.get('version')} is not currently live",
            )
        return operation

    def _fail_closed(self, projection_id: str, reason: str) -> None:
        with self._guard(projection_id) as session:
            row = session.get(Projection, projection_id)
            if row is None or row.status == "failed":
                raise ClaimCoreError(
                    409, "PROJECTION_FAILED", f"Projection {projection_id} failed: {reason}"
                )
            row.status = "failed"
            self._emit(
                session,
                claim_id=row.claim_id,
                event_type="projection.failed",
                payload={
                    "projection_id": row.id,
                    "operation": row.operation,
                    "mode": row.mode,
                    "reason": reason,
                },
                actor=ACTOR,
            )
        raise ClaimCoreError(
            409, "PROJECTION_FAILED", f"Projection {projection_id} failed: {reason}"
        )

    # -- request ---------------------------------------------------------------

    def request(
        self,
        *,
        claim_id: str,
        operation: str,
        actor: str,
        source_event_id: str | None = None,
    ) -> ProjectionResult:
        """Create one projection from durable sources, or block visibly."""

        if operation not in self.operations:
            raise ClaimCoreError(
                422, "UNKNOWN_OPERATION", f"Operation {operation!r} is not registered"
            )
        definition = self.operations.get(operation)
        if definition.status != "live":
            return ProjectionResult(
                status="blocked_on_inputs",
                operation=operation,
                blocked_on=definition.blocked_on,
            )
        try:
            snapshot = self.paste.build_snapshot(
                claim_id, definition, source_event_id=source_event_id
            )
        except SnapshotBlocked as blocked:
            return ProjectionResult(
                status="blocked_on_inputs",
                operation=operation,
                blocked_on=blocked.blocked_on,
            )
        key = snapshot.idempotency_key(claim_id, operation)
        existing = self._by_idempotency_key(key)
        if existing is not None:
            return ProjectionResult(
                status="exists", operation=operation, projection_id=existing
            )
        projection_id = new_ulid()
        try:
            with self.sessions.begin() as session:
                session.add(
                    Projection(
                        id=projection_id,
                        claim_id=claim_id,
                        operation=operation,
                        mode=definition.mode,
                        status="queued",
                        payload=snapshot.payload,
                        readback=None,
                        divergence=None,
                        evidence=None,
                        attempts=0,
                        idempotency_key=key,
                        created_at=self.clock(),
                        completed_at=None,
                    )
                )
                session.flush()
        except IntegrityError:
            duplicate = self._by_idempotency_key(key)
            if duplicate is None:
                raise
            return ProjectionResult(
                status="exists", operation=operation, projection_id=duplicate
            )
        return ProjectionResult(
            status="created", operation=operation, projection_id=projection_id
        )

    def _by_idempotency_key(self, key: str) -> str | None:
        with self.sessions() as session:
            return session.scalar(
                select(Projection.id).where(Projection.idempotency_key == key)
            )

    # -- event consumption -----------------------------------------------------

    def consume(self, event: Any) -> None:
        """Consume `projection.requested`; client values are never trusted."""

        if event.type != "projection.requested":
            return
        payload = _json(event.payload)
        if not isinstance(payload, dict):
            return
        claim_id = payload.get("claim_id") or event.claim_id
        if not isinstance(claim_id, str):
            return
        if set(payload) == LEGACY_RESERVE_KEYS:
            self._consume_legacy_reserve(event, payload, claim_id)
            return
        operation = payload.get("operation")
        if not isinstance(operation, str) or operation not in self.operations:
            # Unknown operations are rejected and never stored.
            return
        self.request(
            claim_id=claim_id,
            operation=operation,
            actor=ACTOR,
            source_event_id=event.id,
        )

    def _consume_legacy_reserve(
        self, event: Any, payload: dict[str, Any], claim_id: str
    ) -> None:
        """Map only the exact PACKET-17 shape, after verifying durable sources."""

        calc_run_id = payload.get("calc_run_id")
        reserve_total = payload.get("reserve_total")
        if not isinstance(calc_run_id, str) or not isinstance(reserve_total, int):
            return
        with self.app.state.engine.connect() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT id, output FROM calc_runs WHERE id = :run_id "
                        "AND claim_id = :claim_id AND calc_id = 'C-02' "
                        "AND status = 'executed'"
                    ),
                    {"run_id": calc_run_id, "claim_id": claim_id},
                )
                .mappings()
                .first()
            )
        if run is None or _json(run["output"]) != reserve_total:
            return
        current = self.claims.snapshot_current_fields(claim_id, ["reserve.total"]).get(
            "reserve.total"
        )
        source_ref = current.source_ref if current is not None else None
        if (
            current is None
            or current.value != reserve_total
            or not isinstance(source_ref, dict)
            or source_ref.get("calc_run_id") != calc_run_id
        ):
            return
        self.request(
            claim_id=claim_id,
            operation=LEGACY_RESERVE_OPERATION,
            actor=ACTOR,
            source_event_id=event.id,
        )

    def backfill(self, *, actor: str = "system") -> int:
        """Replay `projection.requested` history and resume any verifying row."""

        if not isinstance(actor, str) or not actor:
            raise ValueError("backfill actor is required")
        created = 0
        for row in self._events(None, "projection.requested"):
            event = _RecordedEvent(
                id=str(row["id"]),
                claim_id=row["claim_id"],
                type="projection.requested",
                payload=row["payload"],
            )
            before = self._count()
            self.consume(event)
            created += self._count() - before
        self.resume(actor=actor)
        return created

    def _count(self) -> int:
        with self.sessions() as session:
            return int(
                session.scalar(select(func.count()).select_from(Projection)) or 0
            )

    def resume(self, *, actor: str = "system") -> int:
        """Finish every projection stranded in `verifying` after a crash."""

        with self.sessions() as session:
            stranded = list(
                session.scalars(
                    select(Projection.id).where(Projection.status == "verifying")
                )
            )
        resumed = 0
        for projection_id in stranded:
            try:
                self._finalise(projection_id, actor=actor)
            except ClaimCoreError:
                continue
            resumed += 1
        return resumed

    # -- reads -----------------------------------------------------------------

    def _view(self, row: Projection) -> ProjectionView:
        payload = row.payload if isinstance(row.payload, dict) else {}
        definition = payload.get("operation_definition")
        paste = self._evidence(row)
        readback = row.readback if isinstance(row.readback, dict) else {}
        catalogue = (
            self.operations.get(row.operation) if row.operation in self.operations else None
        )
        groups = paste.get("groups")
        return ProjectionView(
            id=row.id,
            claim_id=row.claim_id,
            operation=row.operation,
            capability_id=f"project.{row.operation}",
            mode=row.mode,
            status=row.status,
            snapshot_hash=payload.get("snapshot_hash"),
            definition_version=(
                definition.get("version") if isinstance(definition, dict) else None
            ),
            blocked_on=None if catalogue is None else catalogue.blocked_on,
            readback_paths=tuple(readback.get("paths", ())),
            attested_by=paste.get("attested_by"),
            attested_at=paste.get("attested_at"),
            paste_seconds=paste.get("paste_seconds"),
            started_at=paste.get("started_at"),
            groups_done={
                key: bool(value.get("done"))
                for key, value in (groups or {}).items()
                if isinstance(value, dict)
            },
            created_at=_iso(row.created_at),
            completed_at=_iso(row.completed_at),
        )

    def _row(self, projection_id: str, *, claim_id: str | None = None) -> Projection:
        with self.sessions() as session:
            row = session.get(Projection, projection_id)
            if row is None or (claim_id is not None and row.claim_id != claim_id):
                # A cross-claim id is a 404, never an existence leak.
                raise ClaimCoreError(
                    404, "PROJECTION_NOT_FOUND", "Projection was not found"
                )
            session.expunge(row)
            return row

    def get(self, projection_id: str, *, actor: str) -> ProjectionView:
        self.require_read_role(actor)
        return self._view(self._row(projection_id))

    def list_for_claim(self, claim_id: str, *, actor: str) -> list[ProjectionView]:
        self.require_read_role(actor)
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(Projection)
                    .where(Projection.claim_id == claim_id)
                    .order_by(Projection.created_at.desc(), Projection.id.desc())
                )
            )
            for row in rows:
                session.expunge(row)
        return [self._view(row) for row in rows]

    def claim_surface(self, claim_id: str, *, actor: str) -> dict[str, Any]:
        """The Claim-360 Systems payload: catalogue plus this claim's projections."""

        views = self.list_for_claim(claim_id, actor=actor)
        return {
            "operations": self.operations.catalogue(),
            "projections": [view.as_dict() for view in views],
        }

    def paste_view(
        self, claim_id: str, projection_id: str, *, actor: str
    ) -> dict[str, Any]:
        """Render the ordered strip. Reads never start the paste clock (#269)."""

        self.require_read_role(actor)
        row = self._row(projection_id, claim_id=claim_id)
        operation = self._definition(row)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by _definition
        paste = self._evidence(row)
        started_at = paste.get("started_at")
        elapsed = paste.get("paste_seconds")
        if elapsed is None and isinstance(started_at, str):
            elapsed = int(
                (self.clock() - datetime.fromisoformat(started_at)).total_seconds()
            )
        return {
            "projection_id": row.id,
            "claim_id": row.claim_id,
            "operation": row.operation,
            "definition_version": click_path.version,
            "mode": row.mode,
            "status": row.status,
            "groups": self.paste.groups(
                claim_id=claim_id,
                payload=row.payload,
                click_path=click_path,
                evidence=paste,
                actor=actor,
            ),
            "readback_fields": self.paste.readback_fields(click_path),
            "attestation_text": "I entered the values exactly as shown.",
            "started_at": started_at,
            "elapsed_seconds": elapsed,
        }

    # -- paste lifecycle -------------------------------------------------------

    def start(self, claim_id: str, projection_id: str, *, actor: str) -> dict[str, Any]:
        """Record the explicit start that owns the clock. Repeats do not reset it."""

        self.require_operate_role(actor)
        row = self._row(projection_id, claim_id=claim_id)
        operation = self._definition(row)
        if operation.mode != "paste_assist" or row.mode != "paste_assist":
            raise ClaimCoreError(
                409, "PROJECTION_MODE_NOT_PASTE_ASSIST", "This projection is not paste-assist"
            )
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status != "queued":
                # An exact repeat by any authorised actor returns current state.
                return self.paste_view(claim_id, projection_id, actor=actor)
            paste = self._evidence(locked)
            paste.setdefault("started_at", self.clock().isoformat())
            paste.setdefault("started_by", actor)
            paste.setdefault("groups", {})
            self._transition(locked, "executing")
            self._store_evidence(locked, paste)
        return self.paste_view(claim_id, projection_id, actor=actor)

    def set_group(
        self, claim_id: str, projection_id: str, group_id: str, *, done: bool, actor: str
    ) -> dict[str, Any]:
        """Toggle one screen's done state. Reversible until final confirmation."""

        self.require_operate_role(actor)
        row = self._row(projection_id, claim_id=claim_id)
        operation = self._definition(row)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by _definition
        if group_id not in {screen.id for screen in click_path.screens}:
            raise ClaimCoreError(404, "PROJECTION_GROUP_NOT_FOUND", "Group was not found")
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status != "executing":
                # Only an explicit start opens the strip and owns the clock.
                raise ClaimCoreError(
                    409,
                    "PROJECTION_STATE_STALE",
                    f"Group updates require an executing projection, not {locked.status}",
                )
            self._transition(locked, "executing")
            paste = self._evidence(locked)
            groups = dict(paste.get("groups") or {})
            groups[group_id] = {
                "done": bool(done),
                "actor": actor,
                "at": self.clock().isoformat(),
            }
            paste["groups"] = groups
            self._store_evidence(locked, paste)
        return self.paste_view(claim_id, projection_id, actor=actor)

    def confirm(
        self,
        claim_id: str,
        projection_id: str,
        *,
        actor: str,
        idempotency_key: str,
        attested: Any,
        readback: dict[str, Any],
    ) -> dict[str, Any]:
        """Accept the final attestation, then finalise crash-safely."""

        self.require_operate_role(actor)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ClaimCoreError(
                422, "IDEMPOTENCY_KEY_REQUIRED", "A non-empty Idempotency-Key is required"
            )
        row = self._row(projection_id, claim_id=claim_id)
        operation = self._definition(row)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by _definition
        request_hash = hashlib.sha256(
            canonical_json({"attested": attested, "readback": readback}).encode("utf-8")
        ).hexdigest()
        paste = self._evidence(row)
        recorded = paste.get("confirm")
        if isinstance(recorded, dict):
            same_key = recorded.get("idempotency_key") == idempotency_key
            same_body = recorded.get("request_hash") == request_hash
            if same_key and not same_body:
                raise ClaimCoreError(
                    409, "IDEMPOTENCY_CONFLICT", "That key was used with a different body"
                )
            if not same_body:
                raise ClaimCoreError(
                    409,
                    "PROJECTION_ALREADY_COMPLETED",
                    "This projection was already confirmed with a different readback",
                )
            return self._finalise(projection_id, actor=actor)

        if row.status != "executing":
            raise ClaimCoreError(
                409,
                "PROJECTION_STATE_STALE",
                f"Confirm requires an executing projection, not {row.status}",
            )
        if attested is not True:
            raise ClaimCoreError(
                422, "ATTESTATION_REQUIRED", "Attestation must be literal true"
            )
        if not isinstance(readback, dict):
            raise ClaimCoreError(422, "READBACK_MALFORMED", "Readback must be an object")
        groups = paste.get("groups") or {}
        outstanding = sorted(
            screen.id
            for screen in click_path.screens
            if not (
                isinstance(groups.get(screen.id), dict)
                and groups[screen.id].get("done") is True
            )
        )
        if outstanding:
            raise ClaimCoreError(
                422,
                "PROJECTION_GROUPS_INCOMPLETE",
                f"Screens {outstanding} are not marked done",
            )
        checked = self.paste.validate_readback(click_path, readback)
        now = self.clock()
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            current = self._evidence(locked)
            if isinstance(current.get("confirm"), dict):
                raise ClaimCoreError(
                    409, "PROJECTION_STATE_STALE", "This projection was confirmed concurrently"
                )
            started_at = current.get("started_at")
            elapsed = (
                int((now - datetime.fromisoformat(started_at)).total_seconds())
                if isinstance(started_at, str)
                else 0
            )
            current["confirm"] = {
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "readback": dict(checked),
            }
            current["attested_by"] = actor
            current["attested_at"] = now.isoformat()
            current["completed_at"] = now.isoformat()
            current["paste_seconds"] = elapsed
            self._transition(locked, "verifying")
            self._store_evidence(locked, current)
        return self._finalise(projection_id, actor=actor)

    # -- finalisation ----------------------------------------------------------

    def _finalise(self, projection_id: str, *, actor: str) -> dict[str, Any]:
        """Commit readback fields, complete once, and run the owned FSM hop."""

        row = self._row(projection_id)
        if row.status == "completed":
            return self._view(row).as_dict()
        if row.status != "verifying":
            raise ClaimCoreError(
                409, "PROJECTION_STATE_STALE", "Finalisation requires a verifying projection"
            )
        operation = self._definition(row)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by _definition
        paste = self._evidence(row)
        confirm = paste.get("confirm") or {}
        accepted = confirm.get("readback") or {}
        attested_by = paste.get("attested_by")
        attested_at = paste.get("attested_at")
        source_ref = {
            "projection_id": row.id,
            "operation": row.operation,
            "operation_version": click_path.version,
            "attested_by": attested_by,
            "attested_at": attested_at,
        }
        writes: list[FieldWrite] = []
        for path, value in accepted.items():
            existing = self.claims.snapshot_current_fields(row.claim_id, [path]).get(path)
            if existing is not None:
                reference = existing.source_ref if isinstance(existing.source_ref, dict) else {}
                if (
                    existing.verification_state == "human_verified"
                    or reference.get("projection_id") != row.id
                    or existing.value != value
                ):
                    self._readback_conflict(row, path)
                # This projection's own append-only version already exists.
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
            self.claims.write_fields(row.claim_id, writes, attested_by or actor)

        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status == "completed":
                return self._view(locked).as_dict()
            self._transition(locked, "completed")
            locked.completed_at = self.clock()
            locked.readback = {
                "paths": sorted(accepted),
                "values": {
                    path: self.claims.protect_snapshot_value(
                        row.claim_id, path=path, value=value
                    )
                    for path, value in accepted.items()
                },
            }
            self._emit(
                session,
                claim_id=locked.claim_id,
                event_type="projection.completed",
                payload={
                    "projection_id": locked.id,
                    "operation": locked.operation,
                    "mode": locked.mode,
                    "snapshot_hash": (locked.payload or {}).get("snapshot_hash"),
                    "readback_paths": sorted(accepted),
                    "attested_by": attested_by,
                    "attested_at": attested_at,
                    "paste_seconds": paste.get("paste_seconds"),
                },
                actor=attested_by or actor,
            )
            completed = self._view(locked).as_dict()
        self._advance_claim_state(row, operation, accepted, actor=attested_by or actor)
        return completed

    def _readback_conflict(self, row: Projection, path: str) -> None:
        """Stop in `verifying` and raise one visible EXCEPTION. Never supersede."""

        subtype = "projection_readback_conflict"
        if not self._exception_exists(row, subtype):
            with self.sessions.begin() as session:
                self._emit(
                    session,
                    claim_id=row.claim_id,
                    event_type="review.created",
                    payload={
                        "type": "EXCEPTION",
                        "subtype": subtype,
                        "projection_id": row.id,
                        "operation": row.operation,
                        "path": path,
                    },
                    actor=ACTOR,
                )
        raise ClaimCoreError(
            409,
            "PROJECTION_READBACK_CONFLICT",
            f"A conflicting current {path} exists; a human must decide",
        )

    def _exception_exists(self, row: Projection, subtype: str) -> bool:
        return any(
            event["payload"].get("subtype") == subtype
            and event["payload"].get("projection_id") == row.id
            for event in self._events(row.claim_id, "review.created")
            if isinstance(event["payload"], dict)
        )

    def _advance_claim_state(
        self,
        row: Projection,
        operation: Operation,
        accepted: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """Own `REPORT_RECEIVED→REGISTERED` only. Never `REGISTERED→RESERVED`."""

        if operation.id != "icon.claim_register":
            return
        with self.app.state.engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM claims WHERE id = :claim_id"),
                {"claim_id": row.claim_id},
            ).scalar()
        if status == "REPORT_RECEIVED":
            self.claims.transition_claim(
                row.claim_id,
                "REGISTERED",
                {
                    "projection_id": row.id,
                    "operation": operation.id,
                    "readback_paths": sorted(accepted),
                },
                actor,
            )
            return
        if status == "REGISTERED":
            current = self.claims.snapshot_current_fields(
                row.claim_id, ["external.icon.claim_no"]
            ).get("external.icon.claim_no")
            reference = current.source_ref if current is not None else None
            if not isinstance(reference, dict):
                reference = {}
            if reference.get("projection_id") == row.id:
                return
        subtype = "projection_state_mismatch"
        if self._exception_exists(row, subtype):
            return
        with self.sessions.begin() as session:
            self._emit(
                session,
                claim_id=row.claim_id,
                event_type="review.created",
                payload={
                    "type": "EXCEPTION",
                    "subtype": subtype,
                    "projection_id": row.id,
                    "operation": operation.id,
                    "claim_status": status,
                },
                actor=ACTOR,
            )

    # -- weekly readback sampling ---------------------------------------------

    @staticmethod
    def selected_for_readback(projection_id: str, *, rate_percent: int) -> bool:
        """The existing deterministic selector: sha256(projection_id) % 100 < rate."""

        digest = hashlib.sha256(projection_id.encode("utf-8")).hexdigest()
        return int(digest, 16) % 100 < rate_percent

    def sample_paste_readbacks(self, *, actor: str = ACTOR) -> dict[str, Any]:
        """Create at most one `PASTE_READBACK_CHECK` per completed projection."""

        rate = self.operations.sampling.rate_percent
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(Projection)
                    .where(
                        Projection.status == "completed",
                        Projection.mode == "paste_assist",
                    )
                    .order_by(Projection.created_at, Projection.id)
                )
            )
            for row in rows:
                session.expunge(row)
        sampled = {
            event["payload"].get("projection_id")
            for event in self._events(None, "review.created")
            if isinstance(event["payload"], dict)
            and event["payload"].get("type") == "PASTE_READBACK_CHECK"
        }
        created: list[str] = []
        for row in rows:
            if row.id in sampled:
                continue
            if not self.selected_for_readback(row.id, rate_percent=rate):
                continue
            readback = row.readback if isinstance(row.readback, dict) else {}
            with self.sessions.begin() as session:
                self._emit(
                    session,
                    claim_id=row.claim_id,
                    event_type="review.created",
                    payload={
                        "type": "PASTE_READBACK_CHECK",
                        "projection_id": row.id,
                        "operation": row.operation,
                        "capability_id": f"project.{row.operation}",
                        "snapshot_hash": (row.payload or {}).get("snapshot_hash"),
                        "readback_paths": list(readback.get("paths", [])),
                    },
                    actor=actor,
                )
            sampled.add(row.id)
            created.append(row.id)
        return {"scanned": len(rows), "created": len(created), "rate_percent": rate}


@dataclass(frozen=True)
class _RecordedEvent:
    """Minimal event shape used by the history backfill."""

    id: str
    claim_id: str | None
    type: str
    payload: dict[str, Any]


__all__ = [
    "ACTOR",
    "OPERATE_ROLES",
    "ProjectionResult",
    "ProjectionService",
    "ProjectionView",
    "READ_ROLES",
]
