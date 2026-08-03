"""Owner-pinned PACKET-23 contract for Microsoft Graph inbound delivery.

The tests use only an injected client.  A builder must not require a live
tenant, mailbox, network call or credential to satisfy this contract.
"""

from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace


class FakeInboundGraphClient:
    """The complete Graph seam PACKET-23 is allowed to consume."""

    def __init__(self) -> None:
        self.delta_tokens: list[str | None] = []
        self.renewed_client_states: list[str] = []
        self.downloads: list[tuple[str, str]] = []

    def delta_page(self, token: str | None) -> dict:
        self.delta_tokens.append(token)
        return {"messages": [], "next_token": "opaque-delta-token"}

    def renew_subscription(self, client_state: str) -> dict:
        self.renewed_client_states.append(client_state)
        return {
            "subscription_id": "opaque-subscription-id",
            "expires_at": "2026-08-02T00:00:00+00:00",
        }

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self.downloads.append((message_id, attachment_id))
        return b"attachment"


def test_packet_23_exports_only_the_injected_graph_service_surface():
    graph = importlib.import_module("graph_integration")
    assert callable(graph.build_graph_integration)
    signature = inspect.signature(graph.build_graph_integration)
    assert tuple(signature.parameters) == ("app", "client", "config")
    assert signature.parameters["client"].default is None
    assert signature.parameters["config"].default is None

    handle = graph.build_graph_integration(
        SimpleNamespace(state=SimpleNamespace()),
        client=FakeInboundGraphClient(),
        config={},
    )
    assert callable(handle.inbound.delta_once)
    assert callable(handle.inbound.renew_once)
    assert callable(handle.inbound.accept_webhook)


def test_packet_23_missing_credentials_are_visible_and_never_fake_success():
    graph = importlib.import_module("graph_integration")
    handle = graph.build_graph_integration(
        SimpleNamespace(state=SimpleNamespace()),
        config={},
    )
    expected = {
        "status": "blocked_on_inputs",
        "blocked_on": "graph_credentials",
    }
    assert handle.inbound.delta_once() == expected
    assert handle.inbound.renew_once() == expected


def test_packet_23_pins_atomic_delta_privacy_and_webhook_rules_in_code():
    inbound = importlib.import_module("graph_integration.inbound")
    source = inspect.getsource(inbound)

    # The durable event and token advance share a database transaction.  The
    # router remains the already-approved consumer of this exact event.
    assert "email.received" in source
    assert "delta_token" in source
    assert "begin()" in source or "session.begin" in source
    assert "graph_message_id" in source

    # clientState is random verification material stored only as a digest.
    assert "client_state" in source
    assert "compare_digest" in source
    assert "sha256" in source
    assert "client_state=" not in source

    # Oversize mail is an explicit exception, not a truncated or logged body.
    assert "25 * 1024 * 1024" in source or "26_214_400" in source
    assert "inbound_message_too_large" in source
    for forbidden in (
        "logging.info(message",
        "logger.info(message",
        "print(message",
        "body_text=",
        "attachment_bytes=",
    ):
        assert forbidden not in source
