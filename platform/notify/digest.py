"""Idempotent owned-claim digest and its pack-configured Beat entry."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from notify.transports import NotificationWriter


class DigestService:
    def __init__(self, app: Any, config: dict[str, Any], writer: NotificationWriter) -> None:
        self.app = app
        self.config = config
        self.writer = writer

    def _summary(
        self,
        actor: str,
        claim_ids: list[str],
        *,
        archive_start: datetime,
        archive_end: datetime,
    ) -> dict[str, Any]:
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
            archived_mail = connection.execute(
                text(
                    "SELECT COUNT(*) FROM events WHERE type = 'mail.archived' "
                    "AND claim_id IS NULL AND occurred_at >= :start "
                    "AND occurred_at < :end"
                ),
                {"start": archive_start, "end": archive_end},
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
            "archived_mail": int(archived_mail),
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
        timezone = ZoneInfo(digest["timezone"])
        local_date = now.astimezone(timezone).date()
        eat_date = local_date.isoformat()
        archive_start = datetime.combine(local_date, time.min, tzinfo=timezone).astimezone(
            UTC
        )
        archive_end = archive_start + timedelta(days=1)
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
                    "summary": self._summary(
                        actor,
                        claim_ids,
                        archive_start=archive_start,
                        archive_end=archive_end,
                    ),
                },
                channels=tuple(digest["channels"]),
                template=dict(digest["template"]),
            )
            created += len(rows)
        if created:
            self.app.state.dispatcher.dispatch_once(consumers=["ledger"])
        return created
__all__ = ["DigestService"]
