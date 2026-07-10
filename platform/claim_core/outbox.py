"""Synchronous-drivable transactional-outbox dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from claim_core.models import Event, EventDelivery
from claim_core.service import new_ulid, utc_now

Consumer = Callable[[Event], None]
EventRecorder = Callable[..., Event]
MAX_ATTEMPTS = 8


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Dispatcher:
    """Fan out committed events with durable per-consumer delivery state."""

    def __init__(
        self,
        session_factory: sessionmaker,
        event_recorder: EventRecorder,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._event = event_recorder
        self._clock = clock
        self._consumers: dict[str, Consumer] = {}
        self._dispatch_lock = Lock()

    @property
    def consumer_names(self) -> frozenset[str]:
        return frozenset(self._consumers)

    def register_consumer(self, name: str, fn: Consumer) -> None:
        if not name or name in self._consumers:
            raise ValueError(f"consumer {name!r} is already registered or invalid")
        self._consumers[name] = fn

    @staticmethod
    def _retry_due(event: Event, delivery: EventDelivery, now: datetime) -> bool:
        attempts = delivery.attempts or 0
        if attempts == 0:
            return True
        cumulative_delay = min((2**attempts) - 1, 255)
        return now >= _aware(event.occurred_at) + timedelta(seconds=cumulative_delay)

    def _claim_attempt(self, event_id: str, consumer: str) -> Event | None:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            query = select(Event).where(Event.id == event_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            event = session.scalar(query)
            if event is None:
                return None
            if (
                event.type == "ops.alert"
                and event.payload.get("failed_consumer") == consumer
            ):
                return None
            delivery = session.get(EventDelivery, (event_id, consumer))
            if delivery is not None and delivery.status in {"succeeded", "dead_letter"}:
                return None
            if delivery is not None and not self._retry_due(event, delivery, now):
                return None
            if delivery is None:
                delivery = EventDelivery(
                    event_id=event_id,
                    consumer=consumer,
                    status="pending",
                    attempts=1,
                    last_error=None,
                )
                session.add(delivery)
            else:
                delivery.status = "pending"
                delivery.attempts = (delivery.attempts or 0) + 1
            session.flush()
            session.expunge(event)
            return event

    def _succeed(self, event_id: str, consumer: str) -> None:
        with self._sessions.begin() as session:
            delivery = session.get(EventDelivery, (event_id, consumer))
            if delivery is not None:
                delivery.status = "succeeded"
                delivery.last_error = None

    def _fail(self, event: Event, consumer: str, error: Exception) -> None:
        with self._sessions.begin() as session:
            delivery = session.get(EventDelivery, (event.id, consumer))
            if delivery is None:
                return
            delivery.last_error = f"{type(error).__name__}: {error}"[:2000]
            if (delivery.attempts or 0) < MAX_ATTEMPTS:
                delivery.status = "failed"
                return
            delivery.status = "dead_letter"
            self._event(
                session,
                claim_id=event.claim_id,
                event_type="ops.alert",
                payload={
                    "subtype": "event_delivery_dead_letter",
                    "event_id": event.id,
                    "failed_consumer": consumer,
                    "attempts": MAX_ATTEMPTS,
                },
                actor="system",
                correlation_id=new_ulid(),
            )

    def dispatch_once(self, consumers: Iterable[str] | None = None) -> int:
        """Attempt each currently eligible delivery once, in event-sequence order."""

        with self._dispatch_lock:
            return self._dispatch_once(consumers)

    def _dispatch_once(self, consumers: Iterable[str] | None = None) -> int:
        selected = list(self._consumers) if consumers is None else list(consumers)
        unknown = set(selected) - set(self._consumers)
        if unknown:
            raise ValueError(f"unknown consumers: {sorted(unknown)}")
        with self._sessions() as session:
            event_ids = list(session.scalars(select(Event.id).order_by(Event.seq)))
        attempted = 0
        for event_id in event_ids:
            for consumer_name in selected:
                event = self._claim_attempt(event_id, consumer_name)
                if event is None:
                    continue
                attempted += 1
                try:
                    self._consumers[consumer_name](event)
                except Exception as error:  # noqa: BLE001 - isolation is the contract
                    self._fail(event, consumer_name, error)
                else:
                    self._succeed(event.id, consumer_name)
        return attempted
