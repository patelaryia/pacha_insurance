"""Transactional claim creation, field writes, hydration, and timeline reads."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from ulid import ULID

from claim_core.crypto import KeyProvider, decrypt_value, encrypt_value, normalise_blind_index
from claim_core.database import ClaimLocks, acquire_database_claim_lock
from claim_core.dictionary import (
    FIELD_DICTIONARY,
    FieldDefinition,
    requires_encryption,
    value_matches,
)
from claim_core.errors import ClaimCoreError, HumanOverrideProtected
from claim_core.fsm import ClaimState, ClaimStateMachine, TransitionResult
from claim_core.models import Claim, ClaimField, Document, Event, SlaClock
from claim_core.schemas import ClaimCreate, FieldWrite, FieldWriteResult
from claim_core.storage import BlobStore

if TYPE_CHECKING:
    from claim_core.ledger import LedgerWriter

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
        *,
        key_provider: KeyProvider | None = None,
        blob_store: BlobStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._claim_locks = claim_locks
        self._clock = clock
        self._key_provider = key_provider
        self._blob_store = blob_store
        self._ledger: LedgerWriter | None = None
        self.fsm = ClaimStateMachine(
            session_factory,
            claim_locks,
            clock,
            self.record_event,
        )

    def set_ledger(self, ledger: LedgerWriter) -> None:
        """Complete application wiring after the single writer is constructed."""

        self._ledger = ledger

    @staticmethod
    def _claim_or_error(session: Session, claim_id: str) -> Claim:
        claim = session.get(Claim, claim_id)
        if claim is None:
            raise ClaimCoreError(404, "CLAIM_NOT_FOUND", f"Claim {claim_id} was not found")
        return claim

    @staticmethod
    def _next_event_seq(session: Session) -> int:
        return int(session.scalar(select(func.coalesce(func.max(Event.seq), 0))) or 0) + 1

    def record_event(
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
                self.record_event(
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
        definition = FIELD_DICTIONARY.get(write.path)
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
        if (
            definition.allowed_source_types is not None
            and write.source_type not in definition.allowed_source_types
        ):
            raise ClaimCoreError(
                422,
                "SOURCE_TYPE_NOT_ALLOWED",
                f"Field {write.path!r} cannot be written by source type "
                f"{write.source_type!r}",
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
            self.record_event(
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
                stored_value = write.value
                value_search = None
                if requires_encryption(write.path, definition):
                    if self._key_provider is None:
                        raise RuntimeError("PII key provider is not configured")
                    if claim.dek_wrapped is None:
                        dek = self._key_provider.generate_dek()
                        claim.dek_wrapped = self._key_provider.wrap(dek)
                    else:
                        dek = self._key_provider.unwrap(bytes(claim.dek_wrapped))
                    stored_value = encrypt_value(write.value, dek)
                    if definition.blind_index:
                        value_search = self._key_provider.index_hmac(
                            normalise_blind_index(write.path, write.value)
                        )
                row = ClaimField(
                    id=field_id,
                    claim_id=claim_id,
                    path=write.path,
                    value=stored_value,
                    value_type=write.value_type,
                    source_type=write.source_type,
                    source_ref=write.source_ref,
                    confidence=write.confidence,
                    verification_state=write.verification_state,
                    pii_class=definition.pii_class,
                    value_search=value_search,
                    version=version,
                    superseded_by=None,
                    created_by=actor,
                    created_at=self._clock(),
                )
                session.add(row)
                session.flush()
                if prior is not None:
                    prior.superseded_by = field_id
                self.record_event(
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

    def write_document_extractions(
        self,
        claim_id: str,
        document_id: str,
        writes: list[FieldWrite],
        actor: str,
    ) -> list[FieldWriteResult]:
        """Write extraction results once per document/path through the public gate."""

        with self._sessions() as session:
            self._claim_or_error(session, claim_id)
            document = session.get(Document, document_id)
            if document is None or document.claim_id != claim_id:
                raise ClaimCoreError(
                    422,
                    "VALUE_TYPE_MISMATCH",
                    "Extraction document must belong to the target claim",
                )
            already_written = {
                field.path
                for field in session.scalars(
                    select(ClaimField).where(
                        ClaimField.claim_id == claim_id,
                        ClaimField.source_type == "extraction",
                    )
                )
                if isinstance(field.source_ref, dict)
                and field.source_ref.get("document_id") == document_id
            }
        pending = []
        for write in writes:
            if write.source_type != "extraction":
                raise ClaimCoreError(
                    422,
                    "SOURCE_TYPE_NOT_ALLOWED",
                    "write_document_extractions accepts extraction writes only",
                )
            if write.source_ref is None or write.source_ref.get("document_id") != document_id:
                raise ClaimCoreError(
                    422,
                    "VALUE_TYPE_MISMATCH",
                    "Extraction source_ref must identify the source document",
                )
            if write.path not in already_written:
                pending.append(write)
        return self.write_fields(claim_id, pending, actor) if pending else []

    def transition_claim(
        self, claim_id: str, to: str, payload: dict | None, actor: str
    ) -> TransitionResult:
        """Delegate a primary state change to the package's sole FSM gate."""

        return self.fsm.transition(
            claim_id,
            actor=actor,
            correlation_id=new_ulid(),
            to=to,
            payload=payload,
        )

    def decline_claim(self, claim_id: str, reason: str, actor: str) -> TransitionResult:
        """Delegate the decline action to the package's sole FSM gate."""

        return self.fsm.decline(
            claim_id, reason=reason, actor=actor, correlation_id=new_ulid()
        )

    def set_claim_substatus(
        self, claim_id: str, substatus: str | None, actor: str
    ) -> TransitionResult:
        """Delegate a substatus action to the package's sole FSM gate."""

        return self.fsm.set_substatus(
            claim_id,
            substatus=substatus,
            actor=actor,
            correlation_id=new_ulid(),
        )

    @staticmethod
    def _blocked_reasons(session: Session, claim_id: str) -> list[str]:
        review_events = session.scalars(
            select(Event).where(
                Event.claim_id == claim_id,
                Event.type == "review.created",
            )
        )
        if any(
            event.payload.get("subtype") == "decline_approval_required"
            for event in review_events
        ):
            return ["decline pending claims_manager approval"]
        return []

    def hydrate_claim(
        self, claim_id: str, actor: str
    ) -> tuple[Claim, dict[str, ClaimField], list[str]]:
        with self._sessions() as session:
            claim = self._claim_or_error(session, claim_id)
            fields = self._current_fields(session, claim_id)
            blocked_reasons = self._blocked_reasons(session, claim_id)
            wrapped_dek = claim.dek_wrapped
            session.expunge(claim)
            for field in fields.values():
                session.expunge(field)
        encrypted_paths = [
            path
            for path, field in fields.items()
            if requires_encryption(path, FIELD_DICTIONARY[path])
        ]
        if encrypted_paths:
            if self._key_provider is None or wrapped_dek is None or self._ledger is None:
                raise RuntimeError("PII read services are not configured")
            dek = self._key_provider.unwrap(bytes(wrapped_dek))
            for path in encrypted_paths:
                fields[path].value = decrypt_value(fields[path].value, dek)
                self._ledger.append_pii_decrypt(
                    claim_id=claim_id, path=path, actor=actor
                )
        return claim, fields, blocked_reasons

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

    def replay(self, after_seq: int) -> list[Event]:
        """Read the stable external replay window in transport order."""

        watermark = self._clock() - timedelta(seconds=5)
        with self._sessions() as session:
            events = list(
                session.scalars(
                    select(Event)
                    .where(Event.seq > after_seq, Event.occurred_at <= watermark)
                    .order_by(Event.seq)
                    .limit(500)
                )
            )
            for event in events:
                session.expunge(event)
            return events

    def add_document(
        self,
        claim_id: str,
        *,
        filename: str,
        mime: str,
        content: bytes,
        source_channel: str,
        source_ref: str,
        actor: str,
    ) -> Document:
        """Store one immutable original and commit its row + event atomically."""

        if self._blob_store is None:
            raise RuntimeError("blob store is not configured")
        digest = hashlib.sha256(content).hexdigest()
        document_id = new_ulid()
        with self._sessions() as session:
            self._claim_or_error(session, claim_id)
            duplicate = session.scalar(
                select(Document.id)
                .where(Document.claim_id == claim_id, Document.sha256 == digest)
                .limit(1)
            )
        if duplicate is not None:
            raise ClaimCoreError(
                409,
                "DUPLICATE_DOCUMENT",
                "An identical document already exists on this claim",
            )
        key = f"documents/{claim_id}/{document_id}"
        self._blob_store.put(key, content)
        with self._sessions.begin() as session:
            self._claim_or_error(session, claim_id)
            row = Document(
                id=document_id,
                claim_id=claim_id,
                doc_type=None,
                status="received",
                filename=filename,
                mime=mime,
                s3_key=key,
                sha256=digest,
                page_count=None,
                source={"channel": source_channel, "source_ref": source_ref},
                received_at=self._clock(),
            )
            session.add(row)
            self.record_event(
                session,
                claim_id=claim_id,
                event_type="document.received",
                payload={"document_id": document_id, "sha256": digest},
                actor=actor,
                correlation_id=new_ulid(),
            )
            session.flush()
            session.expunge(row)
            return row

    def documents(self, claim_id: str) -> list[Document]:
        with self._sessions() as session:
            self._claim_or_error(session, claim_id)
            rows = list(
                session.scalars(
                    select(Document)
                    .where(Document.claim_id == claim_id)
                    .order_by(Document.received_at, Document.id)
                )
            )
            for row in rows:
                session.expunge(row)
            return rows

    def get_document(self, document_id: str) -> Document:
        """Return detached document metadata through the claim_core boundary."""

        with self._sessions() as session:
            row = session.get(Document, document_id)
            if row is None:
                raise ClaimCoreError(
                    404,
                    "DOCUMENT_NOT_FOUND",
                    f"Document {document_id} was not found",
                )
            session.expunge(row)
            return row

    def set_document_status(
        self,
        document_id: str,
        *,
        status: str | None = None,
        doc_type: str | None = None,
        page_count: int | None = None,
    ) -> Document:
        """Update mutable pipeline metadata without exposing the documents table."""

        if status is None and doc_type is None and page_count is None:
            raise ValueError("at least one document metadata value is required")
        if status is not None and status not in {
            "received",
            "classified",
            "extracted",
            "verified",
            "rejected",
        }:
            raise ValueError(f"unknown document status {status!r}")
        if doc_type is not None and not doc_type:
            raise ValueError("doc_type must not be empty")
        if page_count is not None and page_count < 1:
            raise ValueError("page_count must be positive")
        with self._sessions.begin() as session:
            row = session.get(Document, document_id)
            if row is None:
                raise ClaimCoreError(
                    404,
                    "DOCUMENT_NOT_FOUND",
                    f"Document {document_id} was not found",
                )
            if status is not None:
                row.status = status
            if doc_type is not None:
                row.doc_type = doc_type
            if page_count is not None:
                row.page_count = page_count
            session.flush()
            session.expunge(row)
            return row

    def list_claims(
        self,
        *,
        status: str | None,
        lob: str | None,
        sla_breached: bool | None,
    ) -> list[Claim]:
        if status is not None:
            try:
                ClaimState(status)
            except ValueError as error:
                raise ClaimCoreError(
                    422, "UNKNOWN_STATE", f"Claim state {status!r} is not registered"
                ) from error
        query = select(Claim)
        if status is not None:
            query = query.where(Claim.status == status)
        if lob is not None:
            query = query.where(Claim.lob == lob)
        if sla_breached is True:
            breached_claims = select(SlaClock.claim_id).where(
                SlaClock.state == "breached", SlaClock.stopped_at.is_(None)
            )
            query = query.where(Claim.id.in_(breached_claims))
        elif sla_breached is False:
            breached_claims = select(SlaClock.claim_id).where(
                SlaClock.state == "breached", SlaClock.stopped_at.is_(None)
            )
            query = query.where(Claim.id.not_in(breached_claims))
        query = query.order_by(Claim.created_at, Claim.id)
        with self._sessions() as session:
            rows = list(session.scalars(query))
            for row in rows:
                session.expunge(row)
            return rows
