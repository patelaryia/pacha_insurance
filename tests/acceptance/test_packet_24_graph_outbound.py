"""Owner-pinned PACKET-24 contract for governed Microsoft Graph release."""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace


class FakeOutboundGraphClient:
    """The complete outbound seam; calls are observable and deterministic."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def send_message(self, write_id: str, message: dict) -> dict:
        self.calls.append(("send_message", write_id, message))
        return {"status": "acknowledged", "graph_message_id": "opaque-message-id"}

    def create_upload_session(
        self,
        write_id: str,
        attachment_id: str,
        size: int,
    ) -> dict:
        self.calls.append(("create_upload_session", write_id, attachment_id, size))
        return {"session": "opaque-upload-session", "offset": 0}

    def upload_chunk(
        self,
        session: str,
        offset: int,
        chunk: bytes,
        total: int,
    ) -> dict:
        self.calls.append(("upload_chunk", session, offset, len(chunk), total))
        return {"offset": offset + len(chunk)}

    def probe_write(self, write_id: str) -> dict:
        self.calls.append(("probe_write", write_id))
        return {"status": "not_found"}


def test_packet_24_extends_the_same_handle_with_release_due():
    graph = importlib.import_module("graph_integration")
    handle = graph.build_graph_integration(
        SimpleNamespace(state=SimpleNamespace()),
        client=FakeOutboundGraphClient(),
        config={},
    )
    assert callable(handle.outbound.release_due)
    signature = inspect.signature(handle.outbound.release_due)
    assert tuple(signature.parameters) == ("now",)


def test_packet_24_missing_credentials_leave_release_work_visible():
    graph = importlib.import_module("graph_integration")
    handle = graph.build_graph_integration(
        SimpleNamespace(state=SimpleNamespace()),
        config={},
    )
    assert handle.outbound.release_due(datetime(2026, 7, 30, tzinfo=UTC)) == {
        "status": "blocked_on_inputs",
        "blocked_on": "graph_credentials",
    }


def test_packet_24_pins_throttle_chunk_restart_and_truth_rules_in_code():
    outbound = importlib.import_module("graph_integration.outbound")
    source = inspect.getsource(outbound)

    assert "30" in source
    assert "release_due_at" in source
    assert "write_id" in source
    assert "execute_or_stage" in source
    assert "3 * 1024 * 1024" in source or "3_145_728" in source
    assert "4 * 1024 * 1024" in source or "4_194_304" in source
    assert "upload_chunk" in source
    assert "probe_write" in source
    assert "uncertain_write" in source
    assert "email.sent" in source
    assert "begin()" in source or "session.begin" in source

    for forbidden in (
        "threading.Thread",
        "graph_client.send(",
        "logger.info(message",
        "print(message",
        "recipient=",
        "attachment_bytes=",
    ):
        assert forbidden not in source

