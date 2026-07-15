"""Transactional-outbox consumer for immediate staff notifications."""

from __future__ import annotations

from typing import Any

from notify.rules import AudienceResolver, NotificationRule
from notify.transports import NotificationWriter


class NotificationConsumer:
    """Apply pack rules idempotently to committed domain events."""

    def __init__(
        self,
        rules: tuple[NotificationRule, ...],
        audiences: AudienceResolver,
        writer: NotificationWriter,
    ) -> None:
        self.rules = rules
        self.audiences = audiences
        self.writer = writer

    def _matching(self, event: Any) -> list[NotificationRule]:
        matching = [rule for rule in self.rules if rule.matches(event)]
        if event.type == "sla.escalated":
            role = event.payload.get("escalate_to_role")
            if isinstance(role, str) and role and role != "pending_capture":
                matching.append(
                    NotificationRule(
                        id="sla_escalated",
                        event_type="sla.escalated",
                        audience=f"role:{role}",
                        channels=("in_app", "email"),
                        when={},
                        template={
                            "id": "staff.sla_escalated",
                            "status": "pending_capture",
                            "blocked_on": "open-item-6",
                        },
                    )
                )
        return matching

    def consume(self, event: Any) -> None:
        for rule in self._matching(event):
            recipients = self.audiences.resolve(rule.audience, event.claim_id)
            for recipient in recipients:
                self.writer.create(
                    recipient=recipient,
                    rule_id=rule.id,
                    source_event_id=event.id,
                    event_type=event.type,
                    claim_id=event.claim_id,
                    source_payload=event.payload,
                    channels=rule.channels,
                    template=rule.template,
                )


__all__ = ["NotificationConsumer"]
