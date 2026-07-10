"""PACKET-03 acceptance — PRD-00 §0.3/§0.5/§0.6/§0.7 + ED-6a; §0.8 scenarios (3)(4)(6).

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-03_prd00_completion.md §4.
"""
from __future__ import annotations

import io
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

AGENT = {"X-Actor": "agent:intake"}
USER = {"X-Actor": "user:01JZXY0000000000000000USER"}


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture()
def harness(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    clock = MutableClock()
    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc3.db")
    app = create_app(url, clock=clock)
    return TestClient(app), app, clock


def _claim(client) -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=AGENT
    )
    assert r.status_code == 201
    return r.json()["id"]


def _drain(app, clock, cycles: int = 12) -> None:
    """Dispatch until quiescent, advancing the clock past any backoff."""
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break
        clock.advance(seconds=90)


def _events(client, claim_id, event_type):
    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    return [e for e in tl if e["type"] == event_type]


# --- §0.3 dispatcher + scenario (3) ------------------------------------------------


def test_dispatch_feeds_ledger_and_chain_verifies(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{"path": "reserve.total", "value": 1_000_000_00,
                          "value_type": "money", "source_type": "extraction",
                          "verification_state": "extracted"}]},
        headers=AGENT,
    )
    _drain(app, clock)

    report = app.state.ledger.verify_chain()
    assert report["ok"] is True
    assert report["checked"] >= 2  # claim.created + field.updated at minimum

    with app.state.engine.connect() as conn:
        actions = [
            row[0]
            for row in conn.execute(
                text("SELECT action FROM audit_ledger ORDER BY seq")
            )
        ]
    assert "claim.created" in actions
    assert "field.version" in actions


def test_scenario_3_crash_recovery_no_loss_no_duplicate(harness):
    client, app, clock = harness
    processed: list[str] = []
    calls = {"n": 0}

    def flaky(event) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated worker death mid-dispatch")
        if event.id not in processed:  # idempotent consumer pattern
            processed.append(event.id)

    app.state.dispatcher.register_consumer("flaky_test", flaky)
    claim_id = _claim(client)
    _drain(app, clock)

    # Event survived the crash and was processed exactly once to success.
    assert len(processed) >= 1
    with app.state.engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT status, attempts FROM event_deliveries "
                    "WHERE consumer = 'flaky_test'"
                )
            )
        )
    assert all(status == "succeeded" for status, _ in rows)
    assert any(attempts >= 2 for _, attempts in rows)  # the retry happened

    # A further dispatch never re-invokes succeeded deliveries.
    before = list(processed)
    _drain(app, clock)
    assert processed == before
    del claim_id


def test_dead_letter_after_max_attempts_with_ops_alert(harness):
    client, app, clock = harness

    def always_fails(event) -> None:
        raise RuntimeError("permanently broken consumer")

    app.state.dispatcher.register_consumer("doomed_test", always_fails)
    _claim(client)
    _drain(app, clock, cycles=20)

    with app.state.engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT status, attempts FROM event_deliveries "
                    "WHERE consumer = 'doomed_test'"
                )
            )
        )
        alerts = conn.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'ops.alert'")
        ).scalar()
    assert rows and all(status == "dead_letter" for status, _ in rows)
    assert all(attempts == 8 for _, attempts in rows)  # max 8, then dead-letter
    assert alerts >= 1


# --- §0.3 replay API ----------------------------------------------------------------


def test_replay_watermark_and_seq_order(harness):
    client, app, clock = harness
    _claim(client)

    r = client.get("/events?after_seq=0", headers=AGENT)
    assert r.status_code == 200
    assert r.json()["events"] == []  # younger than the 5s watermark

    clock.advance(seconds=6)
    r = client.get("/events?after_seq=0", headers=AGENT)
    events = r.json()["events"]
    assert events
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)

    r = client.get(f"/events?after_seq={seqs[-1]}", headers=AGENT)
    assert r.json()["events"] == []

    assert client.get("/events?after_seq=bogus", headers=AGENT).status_code == 422


# --- §0.2 external_refs cache --------------------------------------------------------


def test_external_refs_cache_updated_by_sole_consumer(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{"path": "external.icon.claim_no", "value": "ICN-9",
                          "value_type": "string", "source_type": "projection_readback",
                          "verification_state": "system_confirmed"}]},
        headers={"X-Actor": "system"},
    )
    _drain(app, clock)

    with app.state.engine.connect() as conn:
        raw = conn.execute(
            text("SELECT external_refs FROM claims WHERE id = :i"), {"i": claim_id}
        ).scalar()
    refs = raw if isinstance(raw, dict) else __import__("json").loads(raw)
    assert refs["external.icon.claim_no"] == "ICN-9"


# --- §0.6 ledger tamper — scenario (4) ------------------------------------------------


def test_scenario_4_tampered_ledger_fails_loudly_and_degrades(harness):
    client, app, clock = harness
    _claim(client)
    _drain(app, clock)
    assert app.state.ledger.verify_chain()["ok"] is True

    with app.state.engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_ledger SET actor = 'attacker' WHERE seq = 1")
        )

    report = app.state.ledger.run_nightly_verification()
    assert report["ok"] is False
    assert report["first_bad_seq"] == 1

    with app.state.engine.connect() as conn:
        degraded = conn.execute(
            text("SELECT value FROM platform_state WHERE key = 'audit_degraded'")
        ).scalar()
        frozen = conn.execute(
            text(
                "SELECT value FROM platform_state "
                "WHERE key = 'autonomy_promotions_frozen'"
            )
        ).scalar()
    assert "true" in str(degraded).lower()
    assert "true" in str(frozen).lower()


# --- §0.5 SLA engine — scenario (6) ---------------------------------------------------


def test_scenario_6_acknowledge_clock_warns_then_breaches(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    _drain(app, clock)  # sla consumer starts the acknowledge clock

    app.state.sla_engine.evaluate(clock())
    assert _events(client, claim_id, "sla.warned") == []

    clock.advance(minutes=31)  # past warn 30m
    app.state.sla_engine.evaluate(clock())
    _drain(app, clock)
    assert len(_events(client, claim_id, "sla.warned")) == 1

    clock.advance(minutes=95)  # past breach 2h total
    app.state.sla_engine.evaluate(clock())
    _drain(app, clock)
    assert len(_events(client, claim_id, "sla.breached")) == 1

    # once each, ever
    app.state.sla_engine.evaluate(clock())
    _drain(app, clock)
    assert len(_events(client, claim_id, "sla.warned")) == 1
    assert len(_events(client, claim_id, "sla.breached")) == 1


def test_suppressing_state_stops_open_clocks(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    _drain(app, clock)

    client.post(f"/claims/{claim_id}/transition", json={"to": "TRIAGED"}, headers=AGENT)
    client.post(
        f"/claims/{claim_id}/decline", json={"reason": "below_excess"}, headers=USER
    )
    _drain(app, clock)

    with app.state.engine.connect() as conn:
        open_clocks = conn.execute(
            text(
                "SELECT COUNT(*) FROM sla_clocks "
                "WHERE claim_id = :i AND stopped_at IS NULL"
            ),
            {"i": claim_id},
        ).scalar()
    assert open_clocks == 0

    clock.advance(hours=3)
    app.state.sla_engine.evaluate(clock())
    _drain(app, clock)
    assert _events(client, claim_id, "sla.breached") == []


def test_approval_dwell_is_blocked_on_inputs(harness):
    _client, app, _clock = harness
    definition = app.state.sla_engine.definitions["approval_dwell"]
    assert definition.status == "blocked_on_inputs"


# --- ED-6a PII encryption -------------------------------------------------------------


def test_pii_encrypted_at_rest_plaintext_on_read_and_access_logged(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    r = client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{"path": "parties.insured.phone", "value": "0722 000 111",
                          "value_type": "string", "source_type": "extraction",
                          "verification_state": "extracted"}]},
        headers=AGENT,
    )
    assert r.status_code == 200

    with app.state.engine.connect() as conn:
        raw_value = str(
            conn.execute(
                text(
                    "SELECT value FROM claim_fields "
                    "WHERE claim_id = :i AND path = 'parties.insured.phone'"
                ),
                {"i": claim_id},
            ).scalar()
        )
        value_search = conn.execute(
            text(
                "SELECT value_search FROM claim_fields "
                "WHERE claim_id = :i AND path = 'parties.insured.phone'"
            ),
            {"i": claim_id},
        ).scalar()
        dek = conn.execute(
            text("SELECT dek_wrapped FROM claims WHERE id = :i"), {"i": claim_id}
        ).scalar()

    assert "0722" not in raw_value  # ciphertext at rest
    assert "__enc__" in raw_value
    assert value_search and len(value_search) == 64  # HMAC-SHA256 hex blind index
    assert dek is not None

    hydrated = client.get(f"/claims/{claim_id}", headers=USER).json()
    assert hydrated["fields"]["parties.insured.phone"]["value"] == "0722 000 111"

    _drain(app, clock)
    with app.state.engine.connect() as conn:
        decrypts = conn.execute(
            text("SELECT COUNT(*) FROM audit_ledger WHERE action = 'pii.decrypt'")
        ).scalar()
    assert decrypts >= 1


def test_non_pii_fields_stay_plaintext(harness):
    client, app, _clock = harness
    claim_id = _claim(client)
    client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{"path": "policy.number", "value": "MAY/MOT/9",
                          "value_type": "string", "source_type": "extraction",
                          "verification_state": "extracted"}]},
        headers=AGENT,
    )
    with app.state.engine.connect() as conn:
        raw = str(
            conn.execute(
                text(
                    "SELECT value FROM claim_fields "
                    "WHERE claim_id = :i AND path = 'policy.number'"
                ),
                {"i": claim_id},
            ).scalar()
        )
    assert "MAY/MOT/9" in raw


def test_no_plaintext_pii_in_timeline_or_replay(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{"path": "parties.insured.phone", "value": "0722 999 888",
                          "value_type": "string", "source_type": "extraction",
                          "verification_state": "extracted"}]},
        headers=AGENT,
    )
    clock.advance(seconds=6)
    timeline = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).text
    replay = client.get("/events?after_seq=0", headers=AGENT).text
    for blob in (timeline, replay):
        assert "0722 999 888" not in blob
        assert "0722999888" not in blob


# --- §0.7 documents + claim list -----------------------------------------------------


def test_document_upload_dedupe_and_event(harness):
    client, app, clock = harness
    claim_id = _claim(client)
    pdf = (b"%PDF-1.4 fake estimate", "estimate.pdf", "application/pdf")

    r = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (pdf[1], io.BytesIO(pdf[0]), pdf[2])},
        data={"source_channel": "email", "source_ref": "msg-123"},
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "received"
    assert len(body["sha256"]) == 64

    r2 = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (pdf[1], io.BytesIO(pdf[0]), pdf[2])},
        data={"source_channel": "email", "source_ref": "msg-124"},
        headers=AGENT,
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "DUPLICATE_DOCUMENT"

    docs = client.get(f"/claims/{claim_id}/documents", headers=AGENT).json()["documents"]
    assert len(docs) == 1
    assert docs[0]["filename"] == "estimate.pdf"
    assert _events(client, claim_id, "document.received")


def test_claim_list_filters(harness):
    client, app, clock = harness
    a = _claim(client)
    b = _claim(client)
    client.post(f"/claims/{a}/transition", json={"to": "TRIAGED"}, headers=AGENT)

    r = client.get("/claims?status=TRIAGED&lob=motor", headers=AGENT)
    ids = [c["id"] for c in r.json()["claims"]]
    assert a in ids and b not in ids

    assert client.get("/claims?status=NOT_A_STATE", headers=AGENT).status_code == 422

    # sla_breached filter
    _drain(app, clock)
    clock.advance(hours=3)
    app.state.sla_engine.evaluate(clock())
    _drain(app, clock)
    r = client.get("/claims?sla_breached=true", headers=AGENT)
    breached_ids = {c["id"] for c in r.json()["claims"]}
    assert {a, b} <= breached_ids
