"""Public AR-5 staff-notification package boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claim_core import Base, Notification
from notify.consumer import NotificationConsumer
from notify.digest import DigestService, configure_digest
from notify.rules import AudienceResolver, load_notify_config, parse_notify_config
from notify.transports import NotificationWriter
from notify.ws import WebSocketHub, install_websocket


class NotifyHandle:
    """Synchronous operational handle pinned by PACKET-12."""

    def __init__(self, digest: DigestService) -> None:
        self._digest = digest

    def run_digest(self, now: Any) -> int:
        return self._digest.run(now)


def build_notify(
    app: Any,
    *,
    roles: dict[str, str] | None = None,
    config: str | Path | dict[str, Any] | None = None,
) -> NotifyHandle:
    """Build notification projection, websocket delivery, and digest scheduling."""

    if not hasattr(app.state, "review_queue"):
        raise RuntimeError("build_notify requires build_review_queue")
    repo = Path(__file__).resolve().parents[2]
    configured_roles = dict(app.state.review_queue.roles) if roles is None else dict(roles)
    if config is None:
        payload, rules = load_notify_config(repo / "packs/motor/notify/notify.yaml")
    elif isinstance(config, (str, Path)):
        payload, rules = load_notify_config(config)
    elif isinstance(config, dict):
        payload, rules = parse_notify_config(config)
    else:
        raise TypeError("notify config must be a path, mapping, or None")
    Base.metadata.create_all(app.state.engine, tables=[Notification.__table__])
    hub = WebSocketHub()
    writer = NotificationWriter(app, payload, hub)
    consumer = NotificationConsumer(
        rules,
        AudienceResolver(app.state.engine, configured_roles),
        writer,
    )
    digest = DigestService(app, payload, writer)
    handle = NotifyHandle(digest)
    app.state.notify = handle
    app.state.notify_config = payload
    app.state.notify_roles = configured_roles
    app.state.dispatcher.register_consumer("notify", consumer.consume)
    install_websocket(app, hub)
    configure_digest(digest, payload)
    return handle


__all__ = ["NotifyHandle", "build_notify"]
