"""Synchronous-drivable transactional-outbox dispatcher.

Master plan §14 adds an asynchronous entry point beside the existing
synchronous one rather than replacing it. The Temporal bridge consumer has to
`await` an SDK acknowledgement before its delivery may be marked succeeded, and
an awaited call cannot be driven from `dispatch_once`; every other consumer is
ordinary synchronous SQLAlchemy work and stays exactly as it was.

Both entry points share one dispatch exclusion and one candidate query, so the
claim/success/failure semantics — retry timing, dead-lettering, `ops.alert`
suppression for the consumer that caused it — have a single implementation and
cannot drift apart.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import sessionmaker

from claim_core.models import Event, EventDelivery
from claim_core.service import new_ulid, utc_now

Consumer = Callable[[Event], None] | Callable[[Event], Awaitable[None]]
EventRecorder = Callable[..., Event]
MAX_ATTEMPTS = 8

#: Master plan §16 — a bounded drain batch. The Workflow caps the number of
#: batches; this caps one batch, and together they bound one execution.
MAX_DISPATCH_LIMIT = 500

#: A non-blocking lock probe is intentionally tiny: it keeps the synchronous
#: and asynchronous entry points on the same `threading.Lock` without parking an
#: uncancellable `to_thread(lock.acquire)` call that could acquire the lock after
#: its awaiting Activity has already been cancelled.
_DISPATCH_LOCK_POLL_SECONDS = 0.01


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _validate_limit(limit: int | None) -> int | None:
    """Refuse anything that is not `None` or a batch size in 1..500.

    Booleans are refused explicitly: `True` is an `int` in Python and would
    otherwise silently become a batch of one.
    """

    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer or None")
    if not 1 <= limit <= MAX_DISPATCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_DISPATCH_LIMIT}")
    return limit


def _is_async_consumer(fn: Consumer) -> bool:
    """True when `fn` must be awaited on the event loop rather than threaded.

    Covers both `async def` functions and objects whose `__call__` is one, which
    is the shape `TemporalIntentConsumer` has.
    """

    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)  # noqa: B004 - the bound method is the target
    return call is not None and inspect.iscoroutinefunction(call)


async def _run_sync_to_completion(fn: Callable[..., object], /, *args, **kwargs):
    """Run synchronous work off-loop without abandoning it on cancellation.

    Cancelling an `asyncio.to_thread` await does not stop the worker thread. If
    the dispatcher released its exclusion immediately, a second pass could
    claim the same delivery while the first SQLAlchemy call or consumer was
    still running. Shield the thread task and delay cancellation propagation
    until it has actually stopped touching dispatcher state.
    """

    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated cancellation must not let the dispatcher exclusion
                # go while the synchronous operation is still in flight.
                continue
        # Retrieve any background exception so asyncio does not report an
        # unobserved Task failure; cancellation remains the caller-visible fact.
        with suppress(BaseException):
            task.result()
        raise cancelled


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

    def dispatch_once(
        self,
        consumers: Iterable[str] | None = None,
        *,
        limit: int | None = None,
    ) -> int:
        """Attempt each currently eligible delivery once, in event-sequence order.

        Args:
            consumers: the consumer names to drive; every registered consumer
                when omitted.
            limit: the maximum number of claimed delivery rows to attempt.
                `None` keeps the historical unbounded behaviour.

        Returns:
            The number of delivery rows actually claimed and attempted.

        Raises:
            ValueError: an unknown consumer name or an out-of-range limit.
            TypeError: a selected consumer is asynchronous. Those are driven
                only by `dispatch_once_async`.
        """

        _validate_limit(limit)
        with self._dispatch_lock:
            return self._dispatch_once(consumers, limit=limit)

    async def dispatch_once_async(
        self,
        consumers: Iterable[str] | None = None,
        *,
        limit: int | None = None,
    ) -> int:
        """Drive one bounded pass, awaiting asynchronous consumers directly.

        Every synchronous database and consumer call is moved off the event
        loop. The shared `threading.Lock` is acquired with cancellation-safe
        non-blocking probes: an abandoned `to_thread(lock.acquire)` could acquire
        the lock after its Activity had gone away and wedge every later pass.
        """

        _validate_limit(limit)
        while not self._dispatch_lock.acquire(blocking=False):
            await asyncio.sleep(_DISPATCH_LOCK_POLL_SECONDS)
        try:
            selected = self._select(consumers)
            attempted = 0
            for event_id, consumer_name in await _run_sync_to_completion(
                self._eligible_pairs, selected, limit=limit
            ):
                if limit is not None and attempted >= limit:
                    break
                event = await _run_sync_to_completion(
                    self._claim_attempt, event_id, consumer_name
                )
                if event is None:
                    continue
                attempted += 1
                consumer = self._consumers[consumer_name]
                try:
                    if _is_async_consumer(consumer):
                        await consumer(event)
                    else:
                        result = await _run_sync_to_completion(consumer, event)
                        if inspect.isawaitable(result):
                            await result
                except Exception as error:  # noqa: BLE001 - isolation is the contract
                    await _run_sync_to_completion(self._fail, event, consumer_name, error)
                else:
                    # Success is written only now: for the Temporal bridge that
                    # is strictly after the SDK acknowledged the start or Signal.
                    await _run_sync_to_completion(self._succeed, event.id, consumer_name)
            return attempted
        finally:
            self._dispatch_lock.release()

    def _select(self, consumers: Iterable[str] | None) -> list[str]:
        selected = list(self._consumers) if consumers is None else list(consumers)
        unknown = set(selected) - set(self._consumers)
        if unknown:
            raise ValueError(f"unknown consumers: {sorted(unknown)}")
        return selected

    def _eligible_pairs(
        self,
        selected: list[str],
        *,
        limit: int | None = None,
    ) -> list[tuple[str, str]]:
        """Prefilter delivery candidates without weakening the locked claim.

        A consumer with no delivery row must still see historical events, so the
        query is an outer join. Terminal rows are discarded in SQL; retry timing
        and self-alert suppression are checked from the bulk result. The later
        `_claim_attempt` remains the concurrency authority and rechecks all state
        under the event-row lock.

        Candidates are ordered globally by `(events.seq, consumer_name)` rather
        than grouped by consumer. With a batch limit, retain at most `limit`
        eligible candidates per consumer: the global first `limit` must be
        contained in those prefixes, while memory stays bounded by
        `limit * len(selected)` instead of the complete backlog.
        """

        if not selected:
            return []
        now = _aware(self._clock())
        candidates: list[tuple[int, str, str]] = []
        with self._sessions() as session:
            for consumer in selected:
                retained = 0
                rows = session.execute(
                    select(Event, EventDelivery)
                    .outerjoin(
                        EventDelivery,
                        and_(
                            EventDelivery.event_id == Event.id,
                            EventDelivery.consumer == consumer,
                        ),
                    )
                    .where(
                        or_(
                            EventDelivery.event_id.is_(None),
                            EventDelivery.status.notin_({"succeeded", "dead_letter"}),
                        )
                    )
                    .order_by(Event.seq)
                ).yield_per(limit or 500)
                for event, delivery in rows:
                    if (
                        event.type == "ops.alert"
                        and event.payload.get("failed_consumer") == consumer
                    ):
                        continue
                    if delivery is not None and not self._retry_due(event, delivery, now):
                        continue
                    candidates.append((event.seq, consumer, event.id))
                    retained += 1
                    if limit is not None and retained >= limit:
                        break
        candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
        return [(event_id, consumer) for _seq, consumer, event_id in candidates]

    def _dispatch_once(
        self,
        consumers: Iterable[str] | None = None,
        *,
        limit: int | None = None,
    ) -> int:
        selected = self._select(consumers)
        asynchronous = [name for name in selected if _is_async_consumer(self._consumers[name])]
        if asynchronous:
            names = ", ".join(repr(name) for name in sorted(asynchronous))
            raise TypeError(
                f"consumer(s) {names} are asynchronous and cannot be driven by "
                "dispatch_once; use dispatch_once_async"
            )
        attempted = 0
        for event_id, consumer_name in self._eligible_pairs(selected, limit=limit):
            if limit is not None and attempted >= limit:
                break
            event = self._claim_attempt(event_id, consumer_name)
            if event is None:
                continue
            attempted += 1
            consumer = self._consumers[consumer_name]
            try:
                result = consumer(event)
            except Exception as error:  # noqa: BLE001 - isolation is the contract
                self._fail(event, consumer_name, error)
                continue
            if inspect.isawaitable(result):
                # Closing first: an abandoned coroutine would warn far from here
                # and, worse, would look like a successful synchronous call.
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                error = TypeError(
                    f"consumer {consumer_name!r} is asynchronous and cannot be driven by "
                    "dispatch_once; use dispatch_once_async"
                )
                # A synchronous callable can hide that it returns an awaitable,
                # so this case cannot be rejected before the claim. Persist it as
                # a failed attempt rather than stranding the delivery `pending`.
                self._fail(event, consumer_name, error)
                raise error
            self._succeed(event.id, consumer_name)
        return attempted
