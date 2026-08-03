"""Substantive PACKET-24 durable transport tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import text

from claim_core import create_app, new_ulid
from graph_integration import build_graph_integration


class FakeGraph:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []

    def send_message(self, write_id: str, message: dict) -> dict:
        self.sends.append((write_id, message))
        return {"status": "acknowledged", "graph_message_id": "graph-1"}

    def create_upload_session(self, write_id: str, attachment_id: str, size: int) -> dict:
        return {"session": f"upload-{write_id}-{attachment_id}", "offset": 0}

    def upload_chunk(self, session: str, offset: int, chunk: bytes, total: int) -> dict:
        return {"offset": offset + len(chunk)}

    def probe_write(self, write_id: str) -> dict:
        return {"status": "not_found"}


def test_release_is_durable_idempotent_and_records_truth(tmp_path):
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    app = create_app(f"sqlite:///{tmp_path / 'graph.db'}", clock=lambda: now)
    claim_id = TestClient(app).post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers={"X-Actor": "user:01H00000000000000000000000"},
    ).json()["id"]
    party_id = new_ulid()
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO parties (id, claim_id, role, name, email) "
                "VALUES (:id, :claim_id, 'insured', 'Amina', 'amina@example.test')"
            ),
            {"id": party_id, "claim_id": claim_id},
        )
    app.state.blob_store.put("rendered/body", b"claim update")
    client = FakeGraph()
    graph = build_graph_integration(app, client=client, config={})
    action = SimpleNamespace(
        payload={
            "write_id": "write-1",
            "claim_id": claim_id,
            "actor": "agent:chase",
            "release_due_at": now.isoformat(),
            "template_id": "T-06",
            "to_party_ids": [party_id],
            "attachments": [],
            "blob_key": "rendered/body",
        }
    )
    graph.outbound.enqueue(action)
    graph.outbound.enqueue(action)

    assert graph.outbound.release_due(now) == {
        "status": "completed",
        "released": 1,
        "exceptions": 0,
    }
    assert graph.outbound.release_due(now)["released"] == 0
    assert len(client.sends) == 1
    assert client.sends[0][1]["to"] == ["amina@example.test"]
    with app.state.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM communications WHERE direction = 'outbound'")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'email.sent'")
        ).scalar_one() == 1
