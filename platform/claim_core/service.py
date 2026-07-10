"""Transactional claim creation, field writes, hydration, and timeline reads."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from ulid import ULID

from claim_core.database import ClaimLocks, acquire_database_claim_lock
from claim_core.dictionary import CORE_FIELD_DICTIONARY, FieldDefinition, value_matches
from claim_core.errors import ClaimCoreError, HumanOverrideProtected
from claim_core.models import Claim, ClaimField, Event
from claim_core.schemas import ClaimCreate, FieldWrite, FieldWriteResult

MAX_WRITE_RETRIES = 3
SOURCE_TYPES = frozenset(
    {"extraction", "calc", "rule", "human", "system", "projection_readback"}
)
VERIFICATION_STATES = frozenset(
    {"extracted", "human_verified", "system_confirmed"}
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def new_ulid() -> str:
    """Return a canonical 26-character ULID string."""

    return str(ULID())


class ClaimService:
    """Public engine for Packet-1 claim substrate operations."""

    def __init__(
        self,
        session_factory: sessionmaker,
        claim_locks: ClaimLocks,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._claim_locks = claim_locks
        self._clock = clock

    @staticmethod
    def _claim_or_error(session: Session, claim_id: str) -> Claim:
        claim = session.get(Claim, claim_id)
        if claim is None:
            raise ClaimCoreError(404, "CLAIM_NOT_FOUND", f"Claim {claim_id} was not found")
        return claim

    @staticmethod
    def _next_event_seq(session: Session) -> int:
        return int(session.scalar(select(func.coalesce(func.max(Event.seq), 0))) or 0) + 1

    def _event(
        self,
        session: Session,
        *,
        claim_id: str | None,
        event_type: str,
        payload: dict,
        actor: str,
        correlation_id: str | None,
    ) -> Event:
        values = {}
        if session.bind is None or session.bind.dialect.name != "postgresql":
            values["seq"] = self._next_event_seq(session)
        row = Event(
            id=new_ulid(),
            claim_id=claim_id,
            type=event_type,
            payload=payload,
            actor=actor,
            correlation_id=correlation_id,
            occurred_at=self._clock(),
            **values,
        )
        session.add(row)
        session.flush()
        return row

    def create_claim(self, request: ClaimCreate, actor: str) -> Claim:
        now = self._clock()
        claim = Claim(
            id=new_ulid(),
            lob=request.lob,
            pack_version=request.pack_version,
            status="INTIMATED",
            substatus=None,
            external_refs={},
            dek_wrapped=None,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        with self._claim_locks.acquire(claim.id):
            with self._sessions.begin() as session:
                session.add(claim)
                self._event(
                    session,
                    claim_id=claim.id,
                    event_type="claim.created",
                    payload={
                        "claim_id": claim.id,
                        "lob": claim.lob,
                        "pack_version": claim.pack_version,
                        "status": claim.status,
                    },
                    actor=actor,
                    correlation_id=new_ulid(),
                )
        return claim

    @staticmethod
    def _validate_write(write: FieldWrite, actor: str) -> FieldDefinition:
        definition = CORE_FIELD_DICTIONARY.get(write.path)
        if definition is None:
            raise ClaimCoreError(
                422,
                "FIELD_NOT_IN_DICTIONARY",
                f"Field path {write.path!r} is not registered",
            )
        if write.value_type != definition.value_type:
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Field {write.path!r} requires value_type {definition.value_type!r}",
            )
        if write.source_type not in SOURCE_TYPES:
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Source type {write.source_type!r} is not registered",
            )
        if write.verification_state not in VERIFICATION_STATES:
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Verification state {write.verification_state!r} is not registered",
            )
        if definition.value_type == "money" and (
            not isinstance(write.value, int) or isinstance(write.value, bool)
        ):
            if isinstance(write.value, float):
                raise ClaimCoreError(
                    422,
                    "MONEY_NOT_INTEGER_CENTS",
                    f"Money field {write.path!r} must be a JSON integer in KES cents",
                )
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Value for {write.path!r} does not match type 'money'",
            )
        if not value_matches(definition, write.value):
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Value for {write.path!r} does not match type {definition.value_type!r}",
            )
        if write.pii_class is not None and write.pii_class != definition.pii_class:
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                f"Field {write.path!r} requires pii_class {definition.pii_class!r}",
            )
        if write.verification_state == "human_verified" and not (
            write.source_type == "human" and actor.startswith("user:")
        ):
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                "human_verified requires source_type 'human' and a user actor",
            )
        if write.source_type in {"human", "system"} and write.confidence is not None:
            raise ClaimCoreError(
                422,
                "VALUE_TYPE_MISMATCH",
                "Human and system field sources must not carry confidence",
            )
        return definition

    @staticmethod
    def _current_fields(session: Session, claim_id: str) -> dict[str, ClaimField]:
        rows = session.scalars(
            select(ClaimField).where(
                ClaimField.claim_id == claim_id,
                ClaimField.superseded_by.is_(None),
            )
        )
        return {row.path: row for row in rows}

    def _record_protected_attempt(
        self, claim_id: str, path: str, actor: str, correlation_id: str
    ) -> None:
        with self._sessions.begin() as session:
            acquire_database_claim_lock(session, claim_id)
            self._claim_or_error(session, claim_id)
            self._event(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "type": "EXCEPTION",
                    "subtype": "human_override_attempt",
                    "path": path,
                    "attempted_by": actor,
                },
                actor=actor,
                correlation_id=correlation_id,
            )

    def _write_batch_once(
        self,
        claim_id: str,
        writes: list[FieldWrite],
        definitions: list[FieldDefinition],
        actor: str,
        correlation_id: str,
    ) -> list[FieldWriteResult]:
        with self._sessions.begin() as session:
            acquire_database_claim_lock(session, claim_id)
            claim = self._claim_or_error(session, claim_id)
            current = self._current_fields(session, claim_id)

            prospective_states = {
                path: field.verification_state for path, field in current.items()
            }
            for write in writes:
                prior_state = prospective_states.get(write.path)
                is_human_revision = (
                    actor.startswith("user:")
                    and write.source_type == "human"
                    and write.verification_state == "human_verified"
                )
                if prior_state == "human_verified" and not is_human_revision:
                    raise HumanOverrideProtected(write.path)
                prospective_states[write.path] = write.verification_state

            results = []
            for write, definition in zip(writes, definitions, strict=True):
                prior = current.get(write.path)
                field_id = new_ulid()
                version = 1 if prior is None else prior.version + 1
                row = ClaimField(
                    id=field_id,
                    claim_id=claim_id,
                    path=write.path,
                    value=write.value,
                    value_type=write.value_type,
                    source_type=write.source_type,
                    source_ref=write.source_ref,
                    confidence=write.confidence,
                    verification_state=write.verification_state,
                    pii_class=definition.pii_class,
                    value_search=None,
                    version=version,
                    superseded_by=None,
                    created_by=actor,
                    created_at=self._clock(),
                )
                session.add(row)
                session.flush()
                if prior is not None:
                    prior.superseded_by = field_id
                self._event(
                    session,
                    claim_id=claim_id,
                    event_type="field.updated",
                    payload={"path": write.path, "field_id": field_id, "version": version},
                    actor=actor,
                    correlation_id=correlation_id,
                )
                current[write.path] = row
                results.append(
                    FieldWriteResult(path=write.path, field_id=field_id, version=version)
                )
            claim.updated_at = self._clock()
            return results

    def write_fields(
        self, claim_id: str, writes: list[FieldWrite], actor: str
    ) -> list[FieldWriteResult]:
        definitions = [self._validate_write(write, actor) for write in writes]
        correlation_id = new_ulid()
        with self._claim_locks.acquire(claim_id):
            for attempt in range(MAX_WRITE_RETRIES + 1):
                try:
                    return self._write_batch_once(
                        claim_id, writes, definitions, actor, correlation_id
                    )
                except HumanOverrideProtected as error:
                    self._record_protected_attempt(
                        claim_id, error.path, actor, correlation_id
                    )
                    raise
                except IntegrityError:
                    if attempt == MAX_WRITE_RETRIES:
                        raise
        raise RuntimeError("unreachable write retry state")

    def hydrate_claim(self, claim_id: str) -> tuple[Claim, dict[str, ClaimField]]:
        with self._sessions() as session:
            claim = self._claim_or_error(session, claim_id)
            fields = self._current_fields(session, claim_id)
            session.expunge(claim)
            for field in fields.values():
                session.expunge(field)
            return claim, fields

    def timeline(self, claim_id: str) -> list[Event]:
        with self._sessions() as session:
            self._claim_or_error(session, claim_id)
            events = list(
                session.scalars(
                    select(Event)
                    .where(Event.claim_id == claim_id)
                    .order_by(Event.occurred_at, Event.seq)
                )
            )
            for row in events:
                session.expunge(row)
            return events
