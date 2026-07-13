"""PACKET-01 acceptance — PRD-00 §0.2/§0.3 (write side)/§0.7 subset/§0.8 (1)+(2).

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-01_claim_substrate.md. Runs on SQLite by default;
set DATABASE_URL to run against Postgres (D-4).
"""
from __future__ import annotations

import os
import re
from itertools import count

import pytest

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

AGENT = {"X-Actor": "agent:intake"}
USER = {"X-Actor": "user:01JZXY0000000000000000USER"}


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc.db")
    return TestClient(create_app(url))


def _create_claim(client) -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=AGENT
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


_CITATION_SEQUENCE = count(1)


def _write(client, claim_id, writes, headers=AGENT):
    uncited = [
        write
        for write in writes
        if write.get("verification_state") == "extracted"
        and write.get("source_ref") is None
    ]
    if uncited:
        sequence = next(_CITATION_SEQUENCE)
        document = client.post(
            f"/claims/{claim_id}/documents",
            files={"file": (f"fixture-{sequence}.txt", f"citation {sequence}".encode(),
                            "text/plain")},
            data={"source_channel": "test", "source_ref": f"fixture-{sequence}"},
            headers=headers,
        )
        assert document.status_code == 201, document.text
        document_id = document.json()["id"]
        for write in uncited:
            write["source_ref"] = {
                "document_id": document_id,
                "page": 1,
                "bbox": [0, 0, 1, 1],
                "anchor_text": "fixture citation",
            }
    return client.patch(f"/claims/{claim_id}/fields", json={"writes": writes}, headers=headers)


def _field_write(path, value, value_type, **kw):
    w = {
        "path": path,
        "value": value,
        "value_type": value_type,
        "source_type": kw.pop("source_type", "extraction"),
        "verification_state": kw.pop("verification_state", "extracted"),
    }
    w.update(kw)
    return w


# --- claim creation -----------------------------------------------------------


def test_create_claim_returns_ulid_and_intimated_status(client):
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=AGENT
    )
    assert r.status_code == 201
    body = r.json()
    assert ULID_RE.match(body["id"]), body["id"]
    assert body["status"] == "INTIMATED"
    assert body["lob"] == "motor"
    assert body["pack_version"] == "motor@1.3.0"


def test_create_claim_emits_claim_created_event(client):
    claim_id = _create_claim(client)
    r = client.get(f"/claims/{claim_id}/timeline", headers=AGENT)
    assert r.status_code == 200
    types = [e["type"] for e in r.json()["events"]]
    assert "claim.created" in types


def test_get_unknown_claim_404(client):
    r = client.get("/claims/01JZXY000000000000000NOPE0", headers=AGENT)
    assert r.status_code == 404
    assert r.json()["code"] == "CLAIM_NOT_FOUND"


# --- PRD-00 §0.8 scenario (1): 50 versions incl. supersessions, exact hydration


def test_scenario_1_fifty_versions_hydration_exact(client):
    claim_id = _create_claim(client)

    # 10 paths x 5 versions each = 50 field versions, each superseding the last.
    paths = [
        ("policy.number", "string", lambda i: f"MAY/MOT/{1000 + i}"),
        ("policy.excess", "money", lambda i: 15_000_00 + i),
        ("loss.date", "date", lambda i: f"2026-06-{10 + i:02d}"),
        ("loss.description", "string", lambda i: f"rear-end collision rev {i}"),
        ("intimation.channel", "enum", lambda i: "email"),
        ("intimation.received_at", "datetime", lambda i: f"2026-07-0{1 + i}T08:00:00Z"),
        ("parties.insured.name", "string", lambda i: f"J. Kamau v{i}"),
        ("parties.insured.phone", "string", lambda i: f"+2547000000{i:02d}"),
        ("reserve.total", "money", lambda i: 4_000_000_00 + i * 100),
        ("settlement.amount", "money", lambda i: 1_000_000_00 + i),
    ]
    for i in range(5):
        writes = [_field_write(p, gen(i), vt) for p, vt, gen in paths]
        r = _write(client, claim_id, writes)
        assert r.status_code == 200, r.text

    r = client.get(f"/claims/{claim_id}", headers=AGENT)
    assert r.status_code == 200
    fields = r.json()["fields"]

    assert set(fields.keys()) == {p for p, _, _ in paths}
    for path, _vt, gen in paths:
        f = fields[path]
        assert f["value"] == gen(4), path          # exactly the latest value
        assert f["version"] == 5, path             # five versions written
        # full provenance present
        assert f["source_type"] == "extraction"
        assert f["verification_state"] == "extracted"
        assert f["created_by"] == "agent:intake"
        assert f["created_at"]

    # 50 field.updated events on the timeline
    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    assert sum(1 for e in tl if e["type"] == "field.updated") == 50


# --- PRD-00 §0.8 scenario (2): agent overwrite of human_verified → 409 + review


def test_scenario_2_agent_cannot_supersede_human_verified(client):
    claim_id = _create_claim(client)
    r = _write(client, claim_id, [_field_write("reserve.total", 2_500_000_00, "money")])
    assert r.status_code == 200

    r = _write(
        client,
        claim_id,
        [
            _field_write(
                "reserve.total",
                3_000_000_00,
                "money",
                source_type="human",
                verification_state="human_verified",
            )
        ],
        headers=USER,
    )
    assert r.status_code == 200, r.text

    # Agent attempt → 409 HUMAN_OVERRIDE_PROTECTED, value unchanged, review item event.
    r = _write(client, claim_id, [_field_write("reserve.total", 9_999_999_00, "money")])
    assert r.status_code == 409
    assert r.json()["code"] == "HUMAN_OVERRIDE_PROTECTED"

    f = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]["reserve.total"]
    assert f["value"] == 3_000_000_00
    assert f["verification_state"] == "human_verified"

    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    reviews = [e for e in tl if e["type"] == "review.created"]
    assert len(reviews) == 1
    assert reviews[0]["payload"]["subtype"] == "human_override_attempt"


def test_human_supersedes_agent_is_allowed(client):
    claim_id = _create_claim(client)
    _write(client, claim_id, [_field_write("loss.description", "agent text", "string")])
    r = _write(
        client,
        claim_id,
        [
            _field_write(
                "loss.description",
                "human corrected text",
                "string",
                source_type="human",
                verification_state="human_verified",
            )
        ],
        headers=USER,
    )
    assert r.status_code == 200
    f = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]["loss.description"]
    assert f["value"] == "human corrected text"
    assert f["version"] == 2


# --- dictionary + validation gates ---------------------------------------------


def test_unregistered_path_rejected_422(client):
    claim_id = _create_claim(client)
    r = _write(client, claim_id, [_field_write("vehicle.flux_capacitor", "x", "string")])
    assert r.status_code == 422
    assert r.json()["code"] == "FIELD_NOT_IN_DICTIONARY"


def test_money_float_rejected_422(client):
    claim_id = _create_claim(client)
    r = _write(client, claim_id, [_field_write("reserve.total", 1234.5, "money")])
    assert r.status_code == 422
    assert r.json()["code"] == "MONEY_NOT_INTEGER_CENTS"


def test_value_type_must_match_dictionary(client):
    claim_id = _create_claim(client)
    r = _write(client, claim_id, [_field_write("reserve.total", "a lot", "string")])
    assert r.status_code == 422
    assert r.json()["code"] == "VALUE_TYPE_MISMATCH"


def test_missing_actor_header_rejected(client):
    r = client.post("/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"})
    assert r.status_code == 422


# --- batch atomicity (PRD-00 §0.2 write concurrency) ----------------------------


def test_batch_is_atomic_bad_write_rolls_back_whole_batch(client):
    claim_id = _create_claim(client)
    r = _write(
        client,
        claim_id,
        [
            _field_write("policy.number", "MAY/MOT/77", "string"),
            _field_write("not.a.registered.path", 1, "string"),
        ],
    )
    assert r.status_code == 422
    fields = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]
    assert "policy.number" not in fields  # first write rolled back with the batch

    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    assert not any(e["type"] == "field.updated" for e in tl)  # no event escaped the txn
