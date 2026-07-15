"""Idempotent owned-claim digest and its pack-configured Beat entry."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from celery.schedules import crontab
from sqlalchemy import text

from claim_core import celery_app
from notify.transports import NotificationWriter

_runtime: dict[str, Any] = {}


def _scheduler_time(digest: dict[str, Any]) -> tuple[int, int]:
    """Convert one config-owned wall time to the existing Beat timezone.

    A single daily crontab cannot represent a timezone whose offset from the
    scheduler changes during the year. Refuse that configuration instead of
    silently firing at the wrong local time after a DST boundary.
    """

    source_timezone = ZoneInfo(digest["timezone"])
    scheduler_timezone = celery_app.timezone
    hour = int(digest["hour"])
    minute = int(digest["minute"])
    year = datetime.now(UTC).year
    converted = {
        (
            datetime(year, month, 1, hour, minute, tzinfo=source_timezone)
            .astimezone(scheduler_timezone)
            .hour,
            datetime(year, month, 1, hour, minute, tzinfo=source_timezone)
            .astimezone(scheduler_timezone)
            .minute,
        )
        for month in (1, 4, 7, 10)
    }
    if len(converted) != 1:
        raise ValueError(
            "digest timezone cannot be represented by one scheduler crontab"
        )
    return converted.pop()


class DigestService:
    def __init__(self, app: Any, config: dict[str, Any], writer: NotificationWriter) -> None:
        self.app = app
        self.config = config
        self.writer = writer

    def _summary(self, actor: str, claim_ids: list[str]) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            state_rows = connection.execute(
                text("SELECT status FROM claims WHERE assigned_to = :actor"),
                {"actor": actor},
            )
            states = Counter(row[0] for row in state_rows)
            open_reviews = connection.execute(
                text(
                    "SELECT COUNT(*) FROM review_items "
                    "WHERE assigned_to = :actor AND status = 'open'"
                ),
                {"actor": actor},
            ).scalar_one()
            if claim_ids:
                placeholders = ",".join(f":claim_{index}" for index in range(len(claim_ids)))
                params = {f"claim_{index}": value for index, value in enumerate(claim_ids)}
                clocks = connection.execute(
                    text(
                        "SELECT state, COUNT(*) FROM sla_clocks "
                        f"WHERE stopped_at IS NULL AND claim_id IN ({placeholders}) "
                        "AND state IN ('warned', 'breached') GROUP BY state"
                    ),
                    params,
                )
                clock_counts = {row[0]: row[1] for row in clocks}
            else:
                clock_counts = {}
        return {
            "open_review_items": int(open_reviews),
            "sla": {
                "warned": int(clock_counts.get("warned", 0)),
                "breached": int(clock_counts.get("breached", 0)),
            },
            "claim_states": dict(sorted(states.items())),
        }

    def run(self, now: datetime) -> int:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        digest = self.config["digest"]
        eat_date = now.astimezone(ZoneInfo(digest["timezone"])).date().isoformat()
        with self.app.state.engine.connect() as connection:
            assignments = connection.execute(
                text(
                    "SELECT assigned_to, id FROM claims WHERE assigned_to IS NOT NULL "
                    "ORDER BY assigned_to, id"
                )
            )
            owned: dict[str, list[str]] = {}
            for actor, claim_id in assignments:
                if isinstance(actor, str) and actor.startswith("user:"):
                    owned.setdefault(actor, []).append(claim_id)
        created = 0
        for actor, claim_ids in owned.items():
            rows = self.writer.create(
                recipient=actor,
                rule_id="digest",
                source_event_id=f"digest:{eat_date}:{actor}",
                event_type="notify.daily_digest",
                claim_id=None,
                source_payload={
                    "digest_date": eat_date,
                    "owned_claim_ids": claim_ids,
                    "summary": self._summary(actor, claim_ids),
                },
                channels=tuple(digest["channels"]),
                template=dict(digest["template"]),
            )
            created += len(rows)
        if created:
            self.app.state.dispatcher.dispatch_once(consumers=["ledger"])
        return created


def configure_digest(service: DigestService, config: dict[str, Any]) -> None:
    """Bind the app-owned service and add the data-defined 08:00 EAT schedule."""

    _runtime["service"] = service
    digest = config["digest"]
    scheduler_hour, scheduler_minute = _scheduler_time(digest)
    celery_app.conf.beat_schedule["notify-daily-digest"] = {
        "task": "notify.daily_digest",
        "schedule": crontab(
            hour=scheduler_hour,
            minute=scheduler_minute,
            app=celery_app,
        ),
    }


@celery_app.task(name="notify.daily_digest")
def daily_digest() -> int:
    service = _runtime.get("service")
    if service is None:
        raise RuntimeError("notify digest runtime is not configured")
    return service.run(datetime.now(UTC))


__all__ = ["DigestService", "configure_digest", "daily_digest"]
