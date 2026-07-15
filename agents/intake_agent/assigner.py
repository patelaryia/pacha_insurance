"""Deterministic PRD-05 §5.8 claim assignment consumer."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import column, select, table, update
from sqlalchemy.orm import sessionmaker

from claim_core import STATE_METADATA

SUPPRESSED_STATES = tuple(
    state.value for state, metadata in STATE_METADATA.items() if metadata.suppresses_activity
)
CLAIMS = table(
    "claims",
    column("id"),
    column("status"),
    column("assigned_to"),
)


class ClaimAssigner:
    """Choose the least-loaded officer and ledger the deterministic decision."""

    def __init__(self, app: Any, officers: list[str]) -> None:
        self.app = app
        self.officers = tuple(sorted(set(officers)))
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def consume(self, event: Any) -> None:
        if event.type != "claim.created" or not isinstance(event.claim_id, str):
            return
        with self.sessions.begin() as session:
            claim = session.execute(
                select(CLAIMS.c.id, CLAIMS.c.assigned_to)
                .where(CLAIMS.c.id == event.claim_id)
                .with_for_update()
            ).first()
            if claim is None or claim.assigned_to is not None:
                return
            if not self.officers:
                self.app.state.record_event(
                    session,
                    claim_id=event.claim_id,
                    event_type="review.created",
                    payload={
                        "type": "EXCEPTION",
                        "subtype": "assignment_pool_empty",
                        "claim_id": event.claim_id,
                    },
                    actor="agent:intake",
                    correlation_id=event.id,
                )
                return
            rows = session.scalars(
                select(CLAIMS.c.assigned_to).where(
                    CLAIMS.c.assigned_to.is_not(None),
                    CLAIMS.c.status.not_in(SUPPRESSED_STATES),
                )
            )
            counts = Counter(value for value in rows if value in self.officers)
            selected = min(self.officers, key=lambda officer: (counts[officer], officer))
            session.execute(
                update(CLAIMS)
                .where(CLAIMS.c.id == event.claim_id, CLAIMS.c.assigned_to.is_(None))
                .values(assigned_to=selected)
            )
            self.app.state.record_event(
                session,
                claim_id=event.claim_id,
                event_type="claim.assigned",
                payload={
                    "claim_id": event.claim_id,
                    "assigned_to": selected,
                    "weight": counts[selected],
                    "before": {"assigned_to": None},
                    "after": {"assigned_to": selected},
                },
                actor="agent:intake",
                correlation_id=event.id,
            )


__all__ = ["ClaimAssigner"]
