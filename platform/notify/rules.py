"""Pack-defined internal-notification rules and audience resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text


@dataclass(frozen=True)
class NotificationRule:
    """One validated immediate notification rule."""

    id: str
    event_type: str
    audience: str
    channels: tuple[str, ...]
    when: dict[str, Any]
    template: dict[str, Any]

    def matches(self, event: Any) -> bool:
        return event.type == self.event_type and all(
            event.payload.get(key) == value for key, value in self.when.items()
        )


def parse_notify_config(
    payload: Any,
) -> tuple[dict[str, Any], tuple[NotificationRule, ...]]:
    """Validate already-loaded AR-5 pack configuration."""

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("notify config requires version 1")
    allowlist = payload.get("staff_domain_allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(
        isinstance(domain, str) and domain.strip() for domain in allowlist
    ):
        raise ValueError("notify config requires a staff domain allowlist")
    transport = payload.get("email_transport")
    if not isinstance(transport, dict) or transport.get("status") != "pending_capture":
        raise ValueError("email transport must remain pending_capture until item 1 lands")
    rows = payload.get("rules")
    if not isinstance(rows, list):
        raise ValueError("notify rules must be a list")
    rules: list[NotificationRule] = []
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("notify rule must be a mapping")
        rule_id = row.get("id")
        event_type = row.get("event_type")
        audience = row.get("audience")
        channels = row.get("channels")
        when = row.get("when", {})
        template = row.get("template", {})
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or rule_id in ids
            or not isinstance(event_type, str)
            or not event_type
            or not isinstance(audience, str)
            or not audience
            or not isinstance(channels, list)
            or not channels
            or set(channels) - {"in_app", "email"}
            or not isinstance(when, dict)
            or not isinstance(template, dict)
        ):
            raise ValueError("notify rule contains invalid or duplicate values")
        ids.add(rule_id)
        rules.append(
            NotificationRule(
                id=rule_id,
                event_type=event_type,
                audience=audience,
                channels=tuple(channels),
                when=dict(when),
                template=dict(template),
            )
        )
    return dict(payload), tuple(rules)


def load_notify_config(path: str | Path) -> tuple[dict[str, Any], tuple[NotificationRule, ...]]:
    """Load and fail closed on malformed AR-5 pack configuration."""

    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid notify config: {error}") from error
    return parse_notify_config(payload)


class AudienceResolver:
    """Resolve only the two pack-declared audience forms."""

    def __init__(self, engine: Any, roles: dict[str, str]) -> None:
        self.engine = engine
        self.roles = dict(roles)

    def resolve(self, audience: str, claim_id: str | None) -> list[str]:
        if audience == "assigned_officer":
            if claim_id is None:
                return []
            with self.engine.connect() as connection:
                actor = connection.execute(
                    text("SELECT assigned_to FROM claims WHERE id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalar()
            return [actor] if isinstance(actor, str) and actor.startswith("user:") else []
        prefix = "role:"
        if audience.startswith(prefix):
            role = audience.removeprefix(prefix)
            return sorted(actor for actor, value in self.roles.items() if value == role)
        return []


__all__ = [
    "AudienceResolver",
    "NotificationRule",
    "load_notify_config",
    "parse_notify_config",
]
