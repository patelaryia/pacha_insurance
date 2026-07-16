"""Data-defined, broker-free SLA clock engine."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from claim_core.calendars import Duration, add_duration, load_fixed_holidays
from claim_core.fsm import STATE_METADATA, ClaimState
from claim_core.models import Event, SlaClock, SlaDefinitionRow
from claim_core.service import new_ulid, utc_now

EventRecorder = Callable[..., Event]
_DURATION = re.compile(r"^(\d+)([mhd])$")


@dataclass(frozen=True)
class SlaDefinition:
    id: str
    name: str
    start_event: str
    stop_event: str | None
    stop_filter: dict[str, object]
    key_field: str | None
    warn_after: Duration | None
    breach_after: Duration | None
    escalate_to_role: str
    calendar: str
    status: str


def _parse_duration(value: str | None) -> Duration | None:
    if value is None:
        return None
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid SLA duration {value!r}")
    return Duration(int(match.group(1)), match.group(2))


def _duration_text(value: Duration | None) -> str | None:
    return None if value is None else f"{value.amount}{value.unit}"


def load_definitions(path: str | Path) -> dict[str, SlaDefinition]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    definitions = {}
    for item in payload.get("definitions", []):
        definition = SlaDefinition(
            id=item["id"],
            name=item["name"],
            start_event=item["start_event"],
            stop_event=item.get("stop_event"),
            stop_filter=dict(item.get("stop_filter") or {}),
            key_field=item.get("key_field"),
            warn_after=_parse_duration(item.get("warn_after")),
            breach_after=_parse_duration(item.get("breach_after")),
            escalate_to_role=item["escalate_to_role"],
            calendar=item["calendar"],
            status=item["status"],
        )
        if definition.calendar not in {"24x7", "send_window", "business"}:
            raise ValueError(f"invalid SLA calendar {definition.calendar!r}")
        if definition.key_field is not None and (
            not isinstance(definition.key_field, str) or not definition.key_field
        ):
            raise ValueError(f"invalid SLA key_field {definition.key_field!r}")
        definitions[definition.id] = definition
    return definitions


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SlaEngine:
    """Start, stop, warn, and breach durable clocks synchronously."""

    def __init__(
        self,
        session_factory: sessionmaker,
        event_recorder: EventRecorder,
        *,
        clock: Callable[[], datetime] = utc_now,
        definitions_path: str | Path | None = None,
        holidays_path: str | Path | None = None,
    ) -> None:
        root = Path(__file__).with_name("sla")
        self._sessions = session_factory
        self._event = event_recorder
        self._clock = clock
        self.definitions = load_definitions(
            definitions_path or root / "definitions.yaml"
        )
        self._holidays = load_fixed_holidays(holidays_path or root / "holidays.yaml")
        self._seed_definitions()

    def _seed_definitions(self) -> None:
        with self._sessions.begin() as session:
            for definition in self.definitions.values():
                row = session.get(SlaDefinitionRow, definition.id)
                values = {
                    "name": definition.name,
                    "start_event": definition.start_event,
                    "stop_event": definition.stop_event,
                    "warn_after": _duration_text(definition.warn_after),
                    "breach_after": _duration_text(definition.breach_after),
                    "escalate_to_role": definition.escalate_to_role,
                    "calendar": definition.calendar,
                    "status": definition.status,
                }
                if row is None:
                    session.add(SlaDefinitionRow(id=definition.id, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)

    def _deadline(
        self, started_at: datetime, duration: Duration | None, calendar: str
    ) -> datetime | None:
        if duration is None:
            return None
        return add_duration(started_at, duration, calendar, self._holidays)

    def _start(self, session: Session, event: Event, definition: SlaDefinition) -> None:
        if event.claim_id is None or definition.status == "blocked_on_inputs":
            return
        existing = session.scalar(
            select(SlaClock.id)
            .where(
                SlaClock.definition_id == definition.id,
                SlaClock.started_by_event == event.id,
            )
            .limit(1)
        )
        if existing is not None:
            return
        started_at = _aware(event.occurred_at)
        row = SlaClock(
            id=new_ulid(),
            claim_id=event.claim_id,
            definition_id=definition.id,
            started_at=started_at,
            stopped_at=None,
            warn_at=self._deadline(started_at, definition.warn_after, definition.calendar),
            breach_at=self._deadline(
                started_at, definition.breach_after, definition.calendar
            ),
            state="running",
            started_by_event=event.id,
            stopped_by_event=None,
        )
        session.add(row)
        self._event(
            session,
            claim_id=event.claim_id,
            event_type="sla.started",
            payload={"clock_id": row.id, "definition_id": definition.id},
            actor="system",
            correlation_id=event.correlation_id,
        )

    def _stop_open(
        self,
        session: Session,
        event: Event,
        *,
        definition: SlaDefinition | None = None,
    ) -> None:
        if event.claim_id is None:
            return
        query = select(SlaClock).where(
            SlaClock.claim_id == event.claim_id,
            SlaClock.stopped_at.is_(None),
        )
        if definition is not None:
            query = query.where(SlaClock.definition_id == definition.id)
        clocks = session.scalars(query)
        now = _aware(event.occurred_at)
        for row in clocks:
            if definition is not None and definition.key_field is not None:
                stop_key = event.payload.get(definition.key_field)
                started = session.get(Event, row.started_by_event)
                start_key = (
                    started.payload.get(definition.key_field)
                    if started is not None and isinstance(started.payload, dict)
                    else None
                )
                if stop_key is None or start_key != stop_key:
                    continue
            row.stopped_at = now
            row.stopped_by_event = event.id
            row.state = "stopped"
            self._event(
                session,
                claim_id=row.claim_id,
                event_type="sla.stopped",
                payload={"clock_id": row.id, "definition_id": row.definition_id},
                actor="system",
                correlation_id=event.correlation_id,
            )

    def consume(self, event: Event) -> None:
        """Consume one outbox event idempotently."""

        with self._sessions.begin() as session:
            for definition in self.definitions.values():
                if event.type == definition.start_event:
                    self._start(session, event, definition)
                if (
                    definition.stop_event == event.type
                    and all(
                        event.payload.get(key) == value
                        for key, value in definition.stop_filter.items()
                    )
                ):
                    self._stop_open(session, event, definition=definition)
            should_stop_all = False
            if event.type == "claim.status_changed":
                target = event.payload.get("to")
                try:
                    should_stop_all = STATE_METADATA[ClaimState(target)].suppresses_activity
                except (KeyError, ValueError):
                    pass
            if should_stop_all:
                self._stop_open(session, event)

    def evaluate(self, now: datetime | None = None) -> int:
        """Emit due transitions once and return the number of clock state changes."""

        evaluated_at = _aware(now or self._clock())
        changes = 0
        with self._sessions.begin() as session:
            clocks = list(
                session.scalars(
                    select(SlaClock).where(SlaClock.stopped_at.is_(None))
                )
            )
            for row in clocks:
                warn_at = None if row.warn_at is None else _aware(row.warn_at)
                breach_at = None if row.breach_at is None else _aware(row.breach_at)
                if row.state == "running" and warn_at is not None and evaluated_at >= warn_at:
                    row.state = "warned"
                    changes += 1
                    self._event(
                        session,
                        claim_id=row.claim_id,
                        event_type="sla.warned",
                        payload={"clock_id": row.id, "definition_id": row.definition_id},
                        actor="system",
                        correlation_id=new_ulid(),
                    )
                if row.state in {"running", "warned"} and breach_at is not None and (
                    evaluated_at >= breach_at
                ):
                    row.state = "breached"
                    changes += 1
                    self._event(
                        session,
                        claim_id=row.claim_id,
                        event_type="sla.breached",
                        payload={"clock_id": row.id, "definition_id": row.definition_id},
                        actor="system",
                        correlation_id=new_ulid(),
                    )
        return changes
