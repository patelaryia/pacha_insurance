"""Projection request, snapshot, idempotency, paste-assist, and RPA facade.

Paste-assist is authenticated human work, so it deliberately does not pass
through ``execute_or_stage``. Every *external* write does: PACKET-21 registers
the single deferred executor `projection.rpa.execute` behind the AR-2 gate and
delegates the machinery to `rpa.py`, `reconcile.py`, and `drift.py`. This module
stays the one curated facade the rest of the platform talks to.
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
from projection_agent.drift import DriftEngine
from projection_agent.models import LEGAL_EDGES, Projection
from projection_agent.paste import PasteEngine, SnapshotBlocked, encode_copy_value
from projection_agent.reconcile import Mismatch, ReconciliationEngine, value_digest
from projection_agent.rpa import RUNNER_ACTOR, RpaCoordinator

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

    def __init__(
        self,
        app: Any,
        operations: OperationRegistry,
        *,
        runner_authenticator: Any = None,
    ) -> None:
        self.app = app
        self.operations = operations
        self.claims = app.state.claim_service
        self.clock = app.state.clock
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.paste = PasteEngine(app)
        self._locks = tuple(RLock() for _ in range(LOCK_STRIPES))
        #: Absent until infra supplies one; the internal routes stay unmounted.
        self.runner_authenticator = runner_authenticator
        self.rpa = RpaCoordinator(self)
        self.reconcile = ReconciliationEngine(self)
        self.drift = DriftEngine(self)

    @property
    def rpa_actor(self) -> str:
        """The runner's platform identity. Never a person, never a target login."""

        return RUNNER_ACTOR

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
        if definition.mode == "rpa":
            # A live RPA row goes straight to the AR-2 gate; a blocked or
            # ineligible configuration raises rather than silently pasting.
            self.launch_rpa(projection_id, actor=actor)
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

        if event.type == "review.resolved":
            self.consume_resolution(event)
            return
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
        """Finish verifying rows and any unacknowledged post-completion FSM work."""

        with self.sessions() as session:
            candidates = list(
                session.scalars(
                    select(Projection).where(
                        (Projection.status == "verifying")
                        | (
                            (Projection.status == "completed")
                            & (Projection.operation == "icon.claim_register")
                        )
                    )
                )
            )
            stranded = [
                row.id
                for row in candidates
                if row.status == "verifying"
                or self._evidence(row).get("fsm_checked_at") is None
            ]
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

        if attested is not True:
            raise ClaimCoreError(
                422, "ATTESTATION_REQUIRED", "Attestation must be literal true"
            )
        if not isinstance(readback, dict):
            raise ClaimCoreError(422, "READBACK_MALFORMED", "Readback must be an object")
        checked = self.paste.validate_readback(click_path, readback)
        now = self.clock()
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            current = self._evidence(locked)
            current_confirm = current.get("confirm")
            if isinstance(current_confirm, dict):
                same_key = current_confirm.get("idempotency_key") == idempotency_key
                same_body = current_confirm.get("request_hash") == request_hash
                if same_key and not same_body:
                    raise ClaimCoreError(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "That key was used with a different body",
                    )
                if not same_body:
                    raise ClaimCoreError(
                        409,
                        "PROJECTION_ALREADY_COMPLETED",
                        "This projection was already confirmed with a different readback",
                    )
                # An exact concurrent repeat joins the durable finalisation below.
                return_after_guard = True
            else:
                return_after_guard = False
                if locked.status != "executing":
                    raise ClaimCoreError(
                        409,
                        "PROJECTION_STATE_STALE",
                        f"Confirm requires an executing projection, not {locked.status}",
                    )
                groups = current.get("groups") or {}
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
            if not return_after_guard:
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
            paste = self._evidence(row)
            accepted = (paste.get("confirm") or {}).get("readback") or {}
            self._finish_claim_state(
                row,
                accepted,
                actor=paste.get("attested_by") or actor,
            )
            return self._view(self._row(projection_id)).as_dict()
        if row.status != "verifying":
            raise ClaimCoreError(
                409, "PROJECTION_STATE_STALE", "Finalisation requires a verifying projection"
            )
        operation = self._definition(row)
        click_path = operation.click_path
        assert click_path is not None  # guaranteed by _definition
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            if locked.status == "completed":
                completed = self._view(locked).as_dict()
                completed_row = locked
                completed_paste = self._evidence(locked)
                completed_accepted = (
                    completed_paste.get("confirm") or {}
                ).get("readback") or {}
                completed_actor = completed_paste.get("attested_by") or actor
            else:
                if locked.status != "verifying":
                    raise ClaimCoreError(
                        409,
                        "PROJECTION_STATE_STALE",
                        "Finalisation requires a verifying projection",
                    )
                paste = self._evidence(locked)
                confirm = paste.get("confirm") or {}
                accepted = confirm.get("readback") or {}
                attested_by = paste.get("attested_by")
                attested_at = paste.get("attested_at")
                source_ref = {
                    "projection_id": locked.id,
                    "operation": locked.operation,
                    "operation_version": click_path.version,
                    "attested_by": attested_by,
                    "attested_at": attested_at,
                }
                writes: list[FieldWrite] = []
                for path, value in accepted.items():
                    existing = self.claims.snapshot_current_fields(
                        locked.claim_id, [path]
                    ).get(path)
                    if existing is not None:
                        reference = (
                            existing.source_ref
                            if isinstance(existing.source_ref, dict)
                            else {}
                        )
                        if (
                            existing.verification_state == "human_verified"
                            or reference.get("projection_id") != locked.id
                            or existing.value != value
                        ):
                            self._readback_conflict(locked, path)
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
                    self.claims.write_fields(
                        locked.claim_id, writes, attested_by or actor
                    )
                self._transition(locked, "completed")
                locked.completed_at = self.clock()
                locked.readback = {
                    "paths": sorted(accepted),
                    "values": {
                        path: self.claims.protect_snapshot_value(
                            locked.claim_id, path=path, value=value
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
                completed_row = locked
                completed_accepted = accepted
                completed_actor = attested_by or actor
        self._finish_claim_state(
            completed_row,
            completed_accepted,
            actor=completed_actor,
        )
        return completed

    def _finish_claim_state(
        self,
        row: Projection,
        accepted: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """Complete and acknowledge the post-projection FSM obligation once."""

        if row.operation != "icon.claim_register":
            return
        with self._guard(row.id) as session:
            locked = session.get(Projection, row.id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            paste = self._evidence(locked)
            if paste.get("fsm_checked_at") is not None:
                return
            self._advance_claim_state(locked, locked.operation, accepted, actor=actor)
            paste.setdefault("fsm_checked_at", self.clock().isoformat())
            self._store_evidence(locked, paste)

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
        operation_id: str,
        accepted: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """Own `REPORT_RECEIVED→REGISTERED` only. Never `REGISTERED→RESERVED`."""

        if operation_id != "icon.claim_register":
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
                    "operation": operation_id,
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
                    "operation": operation_id,
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
        created: list[str] = []
        for row in rows:
            if not self.selected_for_readback(row.id, rate_percent=rate):
                continue
            # The completed projection row is the durable uniqueness boundary.
            # PostgreSQL workers serialise on FOR UPDATE; SQLite workers share
            # the application-local stripe. Re-check only after taking it.
            with self._guard(row.id) as session:
                locked = session.get(Projection, row.id)
                if locked is None or locked.status != "completed":
                    continue
                if any(
                    event["payload"].get("type") == "PASTE_READBACK_CHECK"
                    and event["payload"].get("projection_id") == locked.id
                    for event in self._events(locked.claim_id, "review.created")
                    if isinstance(event["payload"], dict)
                ):
                    continue
                readback = (
                    locked.readback if isinstance(locked.readback, dict) else {}
                )
                self._emit(
                    session,
                    claim_id=locked.claim_id,
                    event_type="review.created",
                    payload={
                        "type": "PASTE_READBACK_CHECK",
                        "projection_id": locked.id,
                        "operation": locked.operation,
                        "capability_id": f"project.{locked.operation}",
                        "snapshot_hash": (locked.payload or {}).get("snapshot_hash"),
                        "readback_paths": list(readback.get("paths", [])),
                    },
                    actor=actor,
                )
            created.append(locked.id)
        return {"scanned": len(rows), "created": len(created), "rate_percent": rate}


    # -- PACKET-21 RPA facade --------------------------------------------------

    def capability_level(self, capability_id: str) -> str:
        with self.app.state.engine.connect() as connection:
            level = connection.execute(
                text("SELECT current_level FROM capabilities WHERE id = :id"),
                {"id": capability_id},
            ).scalar()
        if not isinstance(level, str):
            raise ClaimCoreError(
                409, "CAPABILITY_UNKNOWN", f"Capability {capability_id} is not registered"
            )
        return level

    @staticmethod
    def definition_version(row: Projection) -> str | None:
        payload = row.payload if isinstance(row.payload, dict) else {}
        definition = payload.get("operation_definition")
        return definition.get("version") if isinstance(definition, dict) else None

    def review_item(self, review_id: str) -> dict[str, Any] | None:
        with self.app.state.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, type, subtype, status, payload, claim_id, "
                        "resolution, resolution_payload FROM review_items WHERE id = :id"
                    ),
                    {"id": review_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {**dict(row), "payload": _json(row["payload"]),
                "resolution_payload": _json(row["resolution_payload"])}

    def create_exception(
        self, *, claim_id: str | None, subtype: str, payload: dict[str, Any]
    ) -> None:
        """Create one idempotent `EXCEPTION{subtype}` for this projection."""

        projection_id = payload.get("projection_id")
        for event in self._events(claim_id, "review.created"):
            body = event["payload"]
            if (
                isinstance(body, dict)
                and body.get("subtype") == subtype
                and body.get("projection_id") == projection_id
            ):
                return
        with self.sessions.begin() as session:
            self._emit(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "EXCEPTION",
                    "subtype": subtype,
                    **payload,
                },
                actor=ACTOR,
            )

    def launch_rpa(self, projection_id: str, *, actor: str = ACTOR) -> dict[str, Any]:
        """Public entry point: take one queued RPA row through the AR-2 gate."""

        return self.rpa.authorise(projection_id, actor=actor)

    def lease_response(self, grant: Any, *, include_token: bool = True) -> dict[str, Any]:
        """Build the one-time claim response: definition plus ephemeral values."""

        operation = self.operations.get(grant.operation)
        click_path = operation.click_path
        assert click_path is not None  # a lease is only granted for a live path
        row = self._row(grant.projection_id)
        payload = row.payload if isinstance(row.payload, dict) else {}
        values: dict[str, str] = {}
        for entry in payload.get("fields", ()):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            value = entry.get("value")
            if isinstance(path, str) and not path.startswith(("rule:", "literal:")):
                # Every runner decrypt is access-logged against the runner's own
                # platform identity, never a person's.
                value = self.claims.reveal_snapshot_value(
                    row.claim_id, path=path, value=value, actor=self.rpa_actor
                )
            values[str(entry.get("step_id"))] = encode_copy_value(
                value,
                value_type=str(entry.get("value_type")),
                encoding=str(entry.get("external_encoding")),
            )
        body = grant.as_dict(definition=click_path.as_definition(), payload=values)
        if not include_token:
            body.pop("lease_token", None)
        return body

    def rpa_view(self, claim_id: str, projection_id: str, *, actor: str) -> dict[str, Any]:
        """The Systems RPA panel. No selector, credential, or raw blob key."""

        self.require_read_role(actor)
        row = self._row(projection_id, claim_id=claim_id)
        rpa = self.rpa.rpa_evidence(row)
        lease = rpa.get("lease") if isinstance(rpa.get("lease"), dict) else {}
        attempts = [
            {
                "attempt": record.get("attempt"),
                "runner_id": record.get("runner_id"),
                "leased_at": record.get("leased_at"),
                "ended_at": record.get("ended_at"),
                "last_completed_step": record.get("last_completed_step"),
                "write_ids": list(record.get("write_ids") or ()),
                "outcome": record.get("outcome"),
                "reason_code": record.get("reason_code"),
            }
            for record in rpa.get("attempts") or ()
            if isinstance(record, dict)
        ]
        divergence = row.divergence if isinstance(row.divergence, dict) else {}
        catalogue = self.operations.get(row.operation)
        circuit = self.rpa.circuit(row.operation)
        return {
            "projection_id": row.id,
            "claim_id": row.claim_id,
            "operation": row.operation,
            "capability_id": catalogue.capability_id,
            "definition_version": self.definition_version(row),
            "snapshot_hash": (row.payload or {}).get("snapshot_hash"),
            "mode": row.mode,
            "status": row.status,
            "substate": self.substate(row),
            "gate": {
                "state": "staged" if rpa.get("gate") else (
                    "authorised" if rpa.get("authorisation") else "not_requested"
                ),
                "review_id": (rpa.get("gate") or {}).get("review_id"),
            },
            "run_id": rpa.get("run_id"),
            "attempt": int(row.attempts or 0),
            "attempts": attempts,
            "lease": {
                "runner_id": lease.get("runner_id"),
                "expires_at": lease.get("expires_at"),
                "healthy": bool(lease),
            },
            "current_step": (attempts[-1]["last_completed_step"] if attempts else None),
            "evidence": [
                {
                    "evidence_id": frame["evidence_id"],
                    "step_id": frame["step_id"],
                    "phase": frame["phase"],
                    "sha256": frame["sha256"],
                    "captured_at": frame["captured_at"],
                    "attempt": frame.get("attempt"),
                    "url": (
                        f"/console/claims/{row.claim_id}/projections/{row.id}"
                        f"/evidence/{frame['evidence_id']}"
                    ),
                }
                for frame in self.rpa.frames(row)
            ],
            "reconciliation": {
                "status": (
                    "diverged"
                    if row.status == "diverged"
                    else "reconciled" if row.status == "completed" else "pending"
                ),
                "mismatch_paths": [
                    {
                        "path": entry["path"],
                        "kind": entry["kind"],
                        "expected_sha256": entry["expected_sha256"],
                        "actual_sha256": entry["actual_sha256"],
                        "evidence_ids": entry.get("evidence_ids", []),
                    }
                    for entry in divergence.get("paths", ())
                ],
                "detected_by": divergence.get("detected_by"),
            },
            "circuit": {
                "status": "open" if circuit else "closed",
                "reason_code": (circuit or {}).get("reason_code"),
                "definition_version": (circuit or {}).get("definition_version"),
            },
            "fallback": rpa.get("fallback"),
            "terminal": rpa.get("terminal"),
        }

    def substate(self, row: Projection) -> str:
        """The Systems substate label, derived only from durable evidence."""

        rpa = self.rpa.rpa_evidence(row)
        if row.status == "diverged":
            return "diverged"
        if row.status == "failed":
            return "failed"
        if rpa.get("fallback") is not None:
            return "fallback_to_paste"
        if row.status == "verifying":
            return "reconciling"
        if row.status == "executing" and row.mode == "rpa":
            return "running"
        if rpa.get("gate") is not None and rpa.get("authorisation") is None:
            return "awaiting_confirmation"
        if row.status == "completed":
            return "completed"
        return "queued"

    def read_evidence(
        self, claim_id: str, projection_id: str, evidence_id: str, *, actor: str
    ) -> tuple[bytes, str]:
        """Resolve a server-owned evidence key after same-claim RBAC (#284/#295)."""

        self.require_read_role(actor)
        row = self._row(projection_id, claim_id=claim_id)
        frame = next(
            (item for item in self.rpa.frames(row) if item["evidence_id"] == evidence_id),
            None,
        )
        if frame is None:
            raise ClaimCoreError(404, "EVIDENCE_NOT_FOUND", "Evidence was not found")
        content = self.app.state.blob_store.get(frame["key"])
        if hashlib.sha256(content).hexdigest() != frame["sha256"]:
            raise ClaimCoreError(
                409, "EVIDENCE_DIGEST_MISMATCH", "Stored evidence does not match its digest"
            )
        with self.sessions.begin() as session:
            self._emit(
                session,
                claim_id=row.claim_id,
                event_type="pii.decrypted",
                payload={
                    "resource_type": "projection_evidence",
                    "projection_id": row.id,
                    "evidence_id": evidence_id,
                },
                actor=actor,
            )
        return content, frame["sha256"]

    # -- sampled paste readback capture (§12) ---------------------------------

    def capture_paste_readback(
        self,
        review_id: str,
        *,
        actor: str,
        observed: dict[str, Any],
        screenshot: bytes | None = None,
    ) -> dict[str, Any]:
        """Store one officer-observed target capture without copying its values."""

        self.require_operate_role(actor)
        item = self.review_item(review_id)
        if item is None or item["type"] != "PASTE_READBACK_CHECK":
            raise ClaimCoreError(404, "REVIEW_NOT_FOUND", "Review item was not found")
        if item["status"] != "open":
            raise ClaimCoreError(409, "ALREADY_RESOLVED", "Review item is no longer open")
        projection_id = (item["payload"] or {}).get("projection_id")
        if not isinstance(projection_id, str):
            raise ClaimCoreError(
                409, "REVIEW_BLOCKED_ON_INPUTS", "The review names no projection"
            )
        row = self._row(projection_id)
        if row.claim_id != item["claim_id"]:
            raise ClaimCoreError(404, "REVIEW_NOT_FOUND", "Review item was not found")
        if row.status != "completed":
            raise ClaimCoreError(
                409, "PROJECTION_STATE_STALE", "Only a completed projection can be sampled"
            )
        declared = self._declared_capture_keys(row)
        unknown = sorted(set(observed) - set(declared))
        if unknown:
            raise ClaimCoreError(
                422, "READBACK_KEY_UNKNOWN", f"Observed keys {unknown} are not declared"
            )
        missing = sorted(set(declared) - set(observed))
        if missing:
            raise ClaimCoreError(
                422, "READBACK_REQUIRED", f"Observed values {missing} are required"
            )
        for key, value in observed.items():
            if not isinstance(value, str) or not value.strip():
                raise ClaimCoreError(
                    422, "READBACK_MALFORMED", f"Observed {key!r} must be a non-empty string"
                )
        mismatches = self.reconcile.compare_observed(
            row, self._split_observed(row, observed), actor=actor
        )
        capture_id = new_ulid()
        evidence_id = None
        if screenshot:
            evidence_id = self._store_capture_evidence(row, capture_id, screenshot)
        with self._guard(projection_id) as session:
            locked = session.get(Projection, projection_id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            evidence = dict(locked.evidence) if isinstance(locked.evidence, dict) else {}
            captures = dict(evidence.get("paste_readback_checks") or {})
            captures[capture_id] = {
                "review_id": review_id,
                "captured_by": actor,
                "captured_at": self.clock().isoformat(),
                # Values are protected under the claim DEK, never plain.
                "values": {
                    key: self.claims.protect_snapshot_value(
                        locked.claim_id, path=key, value=value
                    )
                    if key in self._declared_capture_keys(locked)
                    else value
                    for key, value in observed.items()
                },
                "mismatch_paths": sorted(mismatch.path for mismatch in mismatches),
                "evidence_id": evidence_id,
            }
            evidence["paste_readback_checks"] = captures
            locked.evidence = evidence
        return {
            "capture_id": capture_id,
            "mismatch_paths": sorted(mismatch.path for mismatch in mismatches),
            "hashes": {
                mismatch.path: {
                    "expected_sha256": value_digest(mismatch.expected),
                    "actual_sha256": value_digest(mismatch.actual),
                }
                for mismatch in mismatches
            },
            "evidence_id": evidence_id,
        }

    def _declared_capture_keys(self, row: Projection) -> list[str]:
        stored = row.readback if isinstance(row.readback, dict) else {}
        return sorted(stored.get("paths", ()))

    def _split_observed(self, row: Projection, observed: dict[str, Any]) -> dict[str, Any]:
        del row
        return {"inputs": {}, "outputs": dict(observed)}

    def _store_capture_evidence(
        self, row: Projection, capture_id: str, content: bytes
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()
        evidence_id = new_ulid()
        key = (
            f"projection-evidence/{row.claim_id}/{row.id}/paste_readback/"
            f"{capture_id}/0001.png"
        )
        self.app.state.blob_store.put(key, content)
        with self._guard(row.id) as session:
            locked = session.get(Projection, row.id)
            if locked is None:
                raise ClaimCoreError(404, "PROJECTION_NOT_FOUND", "Projection was not found")
            rpa = self.rpa.rpa_evidence(locked)
            attempts = [dict(item) for item in (rpa.get("attempts") or ())]
            record = next(
                (item for item in attempts if item.get("attempt") == 0),
                {"attempt": 0, "frames": []},
            )
            frames = [dict(frame) for frame in record.get("frames", [])]
            frames.append(
                {
                    "evidence_id": evidence_id,
                    "step_id": f"paste_readback:{capture_id}",
                    "phase": "after",
                    "sha256": digest,
                    "sequence": len(frames) + 1,
                    "captured_at": self.clock().isoformat(),
                    "key": key,
                }
            )
            record = {**record, "attempt": 0, "frames": frames}
            self.rpa._upsert_attempt(rpa, record)
            self.rpa.store_rpa_evidence(locked, rpa)
        return evidence_id

    def validate_paste_readback_resolution(
        self, item: Any, action: str, payload: dict[str, Any], actor: str
    ) -> None:
        """Enforce §12: an opaque capture id, and no invented match."""

        del actor
        if action == "reject":
            if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
                raise ClaimCoreError(
                    422, "PAYLOAD_INVALID", "A rejected sample requires a reason"
                )
            return
        capture_id = payload.get("capture_id")
        projection_id = (item.payload or {}).get("projection_id")
        if not isinstance(capture_id, str) or not isinstance(projection_id, str):
            raise ClaimCoreError(
                409, "RESOLUTION_BLOCKED_ON_INPUTS", "A stored capture id is required"
            )
        row = self._row(projection_id)
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        capture = (evidence.get("paste_readback_checks") or {}).get(capture_id)
        if not isinstance(capture, dict) or capture.get("review_id") != item.id:
            raise ClaimCoreError(
                409, "PROJECTION_CAPTURE_STALE", "That capture is not this review's"
            )
        mismatches = list(capture.get("mismatch_paths") or ())
        if action == "approve" and mismatches:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "The server comparison is not exact; approval is not available",
            )
        if action == "edit_approve":
            declared = sorted(
                change.get("path")
                for change in (payload.get("diff") or {}).get("typed_changes", ())
                if isinstance(change, dict)
            )
            if declared != sorted(mismatches) or not mismatches:
                raise ClaimCoreError(
                    409,
                    "RESOLUTION_BLOCKED_ON_INPUTS",
                    "The declared diff does not equal the server comparison",
                )

    def consume_resolution(self, event: Any) -> None:
        """Apply resolved projection reviews. Idempotent and replay-safe."""

        if event.type != "review.resolved":
            return
        payload = _json(event.payload)
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "DRAFT_RELEASE":
            self.rpa.consume_review(payload, actor=getattr(event, "actor", ACTOR) or ACTOR)
            return
        if payload.get("type") != "PASTE_READBACK_CHECK":
            return
        review_id = payload.get("review_id")
        item = self.review_item(review_id) if isinstance(review_id, str) else None
        if item is None:
            return
        projection_id = (item["payload"] or {}).get("projection_id")
        if not isinstance(projection_id, str):
            return
        if payload.get("resolution") == "rejected":
            self.create_exception(
                claim_id=item["claim_id"],
                subtype="paste_readback_unavailable",
                payload={
                    "projection_id": projection_id,
                    "review_id": review_id,
                    "reason": payload.get("reason"),
                },
            )
            return
        if payload.get("resolution") != "edited":
            return
        capture_id = payload.get("capture_id")
        row = self._row(projection_id)
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        capture = (evidence.get("paste_readback_checks") or {}).get(capture_id)
        if not isinstance(capture, dict):
            return
        stored = row.readback if isinstance(row.readback, dict) else {}
        values = stored.get("values") if isinstance(stored.get("values"), dict) else {}
        mismatches = []
        for path in capture.get("mismatch_paths") or ():
            expected = self.claims.reveal_snapshot_value(
                row.claim_id, path=path, value=values.get(path), actor=ACTOR
            )
            actual = self.claims.reveal_snapshot_value(
                row.claim_id,
                path=path,
                value=(capture.get("values") or {}).get(path),
                actor=ACTOR,
            )
            mismatches.append(
                Mismatch(path=path, kind="text", expected=expected, actual=actual)
            )
        if not mismatches:
            return
        self.reconcile.diverge(
            projection_id,
            detected_by="paste_sample",
            mismatches=mismatches,
            actor=ACTOR,
            reason_code=f"paste_sample_{capture_id}",
        )

    # -- portfolio metric (§15) ------------------------------------------------

    def divergence_rate(self) -> dict[str, Any]:
        """Point-in-time S-4 series. A zero denominator returns null, not zero."""

        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT status, COUNT(*) FROM projections "
                    "WHERE status IN ('completed', 'diverged') GROUP BY status"
                )
            ).all()
        counts = {status: int(count) for status, count in rows}
        diverged = counts.get("diverged", 0)
        reconciled = counts.get("completed", 0)
        denominator = diverged + reconciled
        return {
            "diverged": diverged,
            "reconciled": reconciled,
            "rate_percent": None if denominator == 0 else round(diverged * 100 / denominator),
            "basis": "current_projection_status",
        }


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
