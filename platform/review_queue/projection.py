"""Idempotent review.created projection consumer and history backfill."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from claim_core import new_ulid
from cop_runtime.contracts import REVIEW_ITEM_TYPES
from review_queue.models import ReviewItem


class ReviewProjection:
    def __init__(self, sessions: sessionmaker) -> None:
        self._sessions = sessions

    def consume(self, event: object) -> None:
        if event.type != "review.created":
            return
        payload = dict(event.payload) if isinstance(event.payload, dict) else {}
        requested_type = payload.get("type")
        known = requested_type in REVIEW_ITEM_TYPES
        item_type = str(requested_type) if known else "EXCEPTION"
        subtype = payload.get("subtype") if known else "unknown_review_type"
        if not isinstance(subtype, str):
            subtype = None
        with self._sessions.begin() as session:
            exists = session.scalar(
                select(ReviewItem.id).where(ReviewItem.source_event_id == event.id)
            )
            if exists is not None:
                return
            assigned_to = None
            if event.claim_id is not None:
                assigned_to = session.execute(
                    text("SELECT assigned_to FROM claims WHERE id = :claim_id"),
                    {"claim_id": event.claim_id},
                ).scalar()
            session.add(
                ReviewItem(
                    id=new_ulid(),
                    claim_id=event.claim_id,
                    type=item_type,
                    subtype=subtype,
                    status="open",
                    payload=payload,
                    source_event_id=event.id,
                    assigned_to=assigned_to,
                    created_at=event.occurred_at,
                )
            )

    def backfill(self, actor: str) -> None:
        """Replay all historical review events; actor is retained for the public contract."""

        if not isinstance(actor, str) or not actor:
            raise ValueError("backfill actor is required")
        with self._sessions() as session:
            rows = session.execute(
                text(
                    "SELECT id, claim_id, type, payload, occurred_at FROM events "
                    "WHERE type = 'review.created' ORDER BY seq"
                )
            ).mappings()
            events = []
            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                values = dict(row)
                values["payload"] = payload
                if isinstance(values["occurred_at"], str):
                    values["occurred_at"] = datetime.fromisoformat(values["occurred_at"])
                events.append(SimpleNamespace(**values))
        for event in events:
            self.consume(event)


__all__ = ["ReviewProjection"]
