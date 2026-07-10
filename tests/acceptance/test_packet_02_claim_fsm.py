"""PACKET-02 acceptance — PRD-00 §0.4 FSM, §0.7 transition API, §0.8 scenario (5).

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-02_claim_fsm.md.
"""
from __future__ import annotations

import os

import pytest

AGENT = {"X-Actor": "agent:intake"}
USER = {"X-Actor": "user:01JZXY0000000000000000USER"}


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc2.db")
    return TestClient(create_app(url))


def _claim(client) -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=AGENT
    )
    assert r.status_code == 201
    return r.json()["id"]


def _go(client, claim_id, to, payload=None, headers=AGENT):
    body = {"to": to}
    if payload is not None:
        body["payload"] = payload
    return client.post(f"/claims/{claim_id}/transition", json=body, headers=headers)


def _status(client, claim_id) -> str:
    return client.get(f"/claims/{claim_id}", headers=AGENT).json()["status"]


def _events(client, claim_id, event_type):
    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    return [e for e in tl if e["type"] == event_type]


def _capture_icon_ref(client, claim_id):
    r = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": "external.icon.claim_no",
                    "value": "ICN-2026-0042",
                    "value_type": "string",
                    "source_type": "projection_readback",
                    "verification_state": "system_confirmed",
                }
            ]
        },
        headers={"X-Actor": "system"},
    )
    assert r.status_code == 200, r.text


# --- happy path: repair lifecycle end to end ------------------------------------


REPAIR_PATH = [
    "TRIAGED",
    "AWAITING_DOCS",
    "IN_ASSESSMENT",
    "REPORT_RECEIVED",
    "REGISTERED",
    "RESERVED",
    "PACK_READY",
    "IN_APPROVAL",
    "APPROVED",
    "IN_REPAIR",
    "REINSPECTION",
    "RELEASED",
    "SETTLEMENT",
    "SETTLED",
    "CLOSED",
]


def test_full_repair_path_transitions_and_events(client):
    claim_id = _claim(client)
    for state in REPAIR_PATH:
        if state == "REGISTERED":
            _capture_icon_ref(client, claim_id)
        r = _go(client, claim_id, state)
        assert r.status_code == 200, f"{state}: {r.text}"
        assert r.json()["status"] == state

    changes = _events(client, claim_id, "claim.status_changed")
    assert len(changes) == len(REPAIR_PATH)
    assert changes[0]["payload"]["from"] == "INTIMATED"
    assert changes[-1]["payload"]["to"] == "CLOSED"


def test_write_off_salvage_path(client):
    claim_id = _claim(client)
    for state in ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"]:
        assert _go(client, claim_id, state).status_code == 200
    for state in ["WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION", "RETAINED", "SETTLEMENT"]:
        r = _go(client, claim_id, state)
        assert r.status_code == 200, f"{state}: {r.text}"
    assert _status(client, claim_id) == "SETTLEMENT"


def test_surrender_checklist_branch(client):
    claim_id = _claim(client)
    for state in [
        "TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED",
        "WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION", "SURRENDER_CHECKLIST",
        "SETTLEMENT",
    ]:
        assert _go(client, claim_id, state).status_code == 200, state


# --- PRD-00 §0.8 scenario (5): illegal transition rejected with reason ----------


def test_scenario_5_illegal_transition_rejected_with_reason(client):
    claim_id = _claim(client)
    r = _go(client, claim_id, "APPROVED")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "ILLEGAL_TRANSITION"
    assert "INTIMATED" in body["detail"]  # names the current state
    assert _status(client, claim_id) == "INTIMATED"


def test_unknown_state_never_guesses(client):
    claim_id = _claim(client)
    r = _go(client, claim_id, "NOT_A_STATE")
    assert r.status_code == 422
    assert r.json()["code"] == "UNKNOWN_STATE"


def test_terminal_state_has_no_outbound_transitions(client):
    claim_id = _claim(client)
    assert _go(client, claim_id, "TRIAGED").status_code == 200
    assert client.post(
        f"/claims/{claim_id}/decline", json={"reason": "below_excess"}, headers=USER
    ).status_code == 200
    r = _go(client, claim_id, "AWAITING_DOCS")
    assert r.status_code == 409


# --- structural guards ------------------------------------------------------------


def test_registered_blocked_without_icon_claim_no(client):
    claim_id = _claim(client)
    for state in ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"]:
        assert _go(client, claim_id, state).status_code == 200
    r = _go(client, claim_id, "REGISTERED")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "TRANSITION_GUARD_BLOCKED"
    assert any("external.icon.claim_no" in b for b in body["blocked_on"])
    # capture the ref, retry succeeds
    _capture_icon_ref(client, claim_id)
    assert _go(client, claim_id, "REGISTERED").status_code == 200


def test_approval_reject_requires_structured_reasons(client):
    claim_id = _claim(client)
    for state in REPAIR_PATH[: REPAIR_PATH.index("IN_APPROVAL") + 1]:
        if state == "REGISTERED":
            _capture_icon_ref(client, claim_id)
        assert _go(client, claim_id, state).status_code == 200
    # reject without reasons → 422
    r = _go(client, claim_id, "PACK_READY")
    assert r.status_code == 422
    assert r.json()["code"] == "REJECT_REASONS_REQUIRED"
    # with structured reasons → 200
    r = _go(
        client, claim_id, "PACK_READY",
        payload={"reasons": [{"code": "quote_stale", "detail": "Quote older than 30d"}]},
    )
    assert r.status_code == 200


# --- decline action ---------------------------------------------------------------


def test_decline_from_triaged_commits(client):
    claim_id = _claim(client)
    assert _go(client, claim_id, "TRIAGED").status_code == 200
    r = client.post(
        f"/claims/{claim_id}/decline", json={"reason": "out_of_cover"}, headers=USER
    )
    assert r.status_code == 200
    assert _status(client, claim_id) == "DECLINED"
    changes = _events(client, claim_id, "claim.status_changed")
    assert changes[-1]["payload"]["to"] == "DECLINED"
    assert changes[-1]["payload"]["reason"] == "out_of_cover"


def test_decline_post_triage_requires_cm_approval_item(client):
    claim_id = _claim(client)
    for state in ["TRIAGED", "AWAITING_DOCS"]:
        assert _go(client, claim_id, state).status_code == 200
    r = client.post(
        f"/claims/{claim_id}/decline", json={"reason": "fraud"}, headers=USER
    )
    assert r.status_code == 202
    assert r.json()["code"] == "APPROVAL_REQUIRED"
    assert _status(client, claim_id) == "AWAITING_DOCS"  # transition did NOT commit

    reviews = _events(client, claim_id, "review.created")
    assert len(reviews) == 1
    assert reviews[0]["payload"]["subtype"] == "decline_approval_required"
    assert reviews[0]["payload"]["reason"] == "fraud"

    hydrated = client.get(f"/claims/{claim_id}", headers=AGENT).json()
    assert any("claims_manager" in b for b in hydrated["blocked_reasons"])


def test_invalid_decline_reason_422(client):
    claim_id = _claim(client)
    assert _go(client, claim_id, "TRIAGED").status_code == 200
    r = client.post(
        f"/claims/{claim_id}/decline", json={"reason": "did_not_like_it"}, headers=USER
    )
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_DECLINE_REASON"


# --- withdrawal / void -------------------------------------------------------------


def test_withdrawn_from_open_pre_settlement_state(client):
    claim_id = _claim(client)
    for state in ["TRIAGED", "AWAITING_DOCS"]:
        assert _go(client, claim_id, state).status_code == 200
    assert _go(client, claim_id, "WITHDRAWN").status_code == 200
    assert _status(client, claim_id) == "WITHDRAWN"


def test_withdrawn_blocked_from_settlement_onwards(client):
    claim_id = _claim(client)
    for state in REPAIR_PATH[: REPAIR_PATH.index("SETTLEMENT") + 1]:
        if state == "REGISTERED":
            _capture_icon_ref(client, claim_id)
        assert _go(client, claim_id, state).status_code == 200
    assert _go(client, claim_id, "WITHDRAWN").status_code == 409


def test_void_only_pre_registered(client):
    a = _claim(client)
    assert _go(client, a, "TRIAGED").status_code == 200
    assert _go(client, a, "VOID").status_code == 200

    b = _claim(client)
    for state in ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"]:
        assert _go(client, b, state).status_code == 200
    _capture_icon_ref(client, b)
    assert _go(client, b, "REGISTERED").status_code == 200
    r = _go(client, b, "VOID")
    assert r.status_code == 409  # after registration, use WITHDRAWN


# --- EX_GRATIA_REVIEW substatus ----------------------------------------------------


def test_ex_gratia_substatus_only_under_declined(client):
    claim_id = _claim(client)
    assert _go(client, claim_id, "TRIAGED").status_code == 200

    r = client.post(
        f"/claims/{claim_id}/substatus", json={"substatus": "EX_GRATIA_REVIEW"}, headers=USER
    )
    assert r.status_code == 409  # not DECLINED yet

    client.post(f"/claims/{claim_id}/decline", json={"reason": "below_excess"}, headers=USER)
    r = client.post(
        f"/claims/{claim_id}/substatus", json={"substatus": "EX_GRATIA_REVIEW"}, headers=USER
    )
    assert r.status_code == 200
    hydrated = client.get(f"/claims/{claim_id}", headers=AGENT).json()
    assert hydrated["status"] == "DECLINED"
    assert hydrated["substatus"] == "EX_GRATIA_REVIEW"
    # plus a review item (PRD-00 §0.4)
    assert any(
        e["payload"].get("subtype") == "ex_gratia_review"
        for e in _events(client, claim_id, "review.created")
    )


# --- rule-linked guards recorded pending (D-6) --------------------------------------


def test_rule_linked_guard_recorded_in_event_payload(client):
    claim_id = _claim(client)
    r = _go(client, claim_id, "TRIAGED")
    assert r.status_code == 200
    change = _events(client, claim_id, "claim.status_changed")[-1]
    assert "coverage+excess evaluated" in change["payload"].get("guards_pending", [])
