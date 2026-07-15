"""Durable in-app delivery and the visibly staged email transport slot."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from claim_core import Notification, new_ulid
from notify.ws import WebSocketHub


class NotificationWriter:
    """Project one source fact into channel rows and ledger events."""

    def __init__(self, app: Any, config: dict[str, Any], hub: WebSocketHub) -> None:
        self.app = app
        self.config = config
        self.hub = hub
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def create(
        self,
        *,
        recipient: str,
        rule_id: str,
        source_event_id: str,
        event_type: str,
        claim_id: str | None,
        source_payload: dict[str, Any],
        channels: tuple[str, ...],
        template: dict[str, Any],
    ) -> list[Notification]:
        created: list[Notification] = []
        for channel in channels:
            with self.sessions.begin() as session:
                existing = session.scalar(
                    select(Notification).where(
                        Notification.recipient == recipient,
                        Notification.event_id == source_event_id,
                        Notification.channel == channel,
                    )
                )
                if existing is not None:
                    continue
                status = "sent" if channel == "in_app" else "staged"
                payload: dict[str, Any] = {
                    "event_type": event_type,
                    "source": dict(source_payload),
                    "template": dict(template),
                }
                if channel == "email":
                    transport = self.config["email_transport"]
                    payload.update(
                        {
                            "transport_status": transport["status"],
                            "blocked_on": transport["blocked_on"],
                        }
                    )
                row = Notification(
                    id=new_ulid(),
                    recipient=recipient,
                    rule_id=rule_id,
                    event_id=source_event_id,
                    claim_id=claim_id,
                    channel=channel,
                    status=status,
                    payload=payload,
                    created_at=self.app.state.clock(),
                    read_at=None,
                )
                session.add(row)
                self.app.state.record_event(
                    session,
                    claim_id=claim_id,
                    event_type=f"notify.{status}",
                    payload={
                        "notification_id": row.id,
                        "recipient": recipient,
                        "rule_id": rule_id,
                        "channel": channel,
                        "source_event_id": source_event_id,
                    },
                    actor="system",
                    correlation_id=row.id,
                )
                session.flush()
                session.expunge(row)
                created.append(row)
            if channel == "in_app":
                self.hub.push(
                    recipient,
                    {
                        "type": "notification",
                        "notification_id": row.id,
                        "event_type": event_type,
                        "claim_id": claim_id,
                    },
                )
        return created


class GraphEmailTransport:
    """Configuration slot; direct Graph delivery stays blocked on open item 1."""

    status = "pending_capture"

    def send(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("email transport is blocked_on open-item-1")


__all__ = ["GraphEmailTransport", "NotificationWriter"]
