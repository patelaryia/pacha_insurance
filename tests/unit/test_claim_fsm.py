"""Unit coverage for the exhaustive PACKET-02 claim lifecycle matrix."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claim_core.app import create_app
from claim_core.dictionary import CORE_FIELD_DICTIONARY
from claim_core.fsm import (
    PRIMARY_TRANSITIONS,
    STATE_METADATA,
    ClaimState,
    ClaimStateMachine,
)

AGENT = {"X-Actor": "agent:intake"}
SYSTEM = {"X-Actor": "system"}
USER = {"X-Actor": "user:01JZXY0000000000000000USER"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app("sqlite://"))


def create_claim(client: TestClient) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers=AGENT,
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_state_set_and_primary_edge_matrix_are_exhaustive() -> None:
    expected_states = {
        "INTIMATED",
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
        "WRITE_OFF",
        "SALVAGE_BIDDING",
        "CLIENT_ELECTION",
        "SURRENDER_CHECKLIST",
        "RETAINED",
        "SETTLEMENT",
        "SETTLED",
        "CLOSED",
        "DECLINED",
        "WITHDRAWN",
        "VOID",
    }
    expected_edges = {
        ("INTIMATED", "TRIAGED"),
        ("TRIAGED", "AWAITING_DOCS"),
        ("AWAITING_DOCS", "IN_ASSESSMENT"),
        ("IN_ASSESSMENT", "REPORT_RECEIVED"),
        ("REPORT_RECEIVED", "WRITE_OFF"),
        ("REPORT_RECEIVED", "REGISTERED"),
        ("REGISTERED", "RESERVED"),
        ("RESERVED", "PACK_READY"),
        ("PACK_READY", "IN_APPROVAL"),
        ("IN_APPROVAL", "APPROVED"),
        ("IN_APPROVAL", "PACK_READY"),
        ("APPROVED", "IN_REPAIR"),
        ("IN_REPAIR", "REINSPECTION"),
        ("REINSPECTION", "RELEASED"),
        ("RELEASED", "SETTLEMENT"),
        ("WRITE_OFF", "SALVAGE_BIDDING"),
        ("SALVAGE_BIDDING", "CLIENT_ELECTION"),
        ("CLIENT_ELECTION", "SURRENDER_CHECKLIST"),
        ("CLIENT_ELECTION", "RETAINED"),
        ("SURRENDER_CHECKLIST", "SETTLEMENT"),
        ("RETAINED", "SETTLEMENT"),
        ("SETTLEMENT", "SETTLED"),
        ("SETTLED", "CLOSED"),
    }
    actual_edges = {
        (source.value, target.value)
        for source, targets in PRIMARY_TRANSITIONS.items()
        for target in targets
    }

    assert {state.value for state in ClaimState} == expected_states
    assert set(PRIMARY_TRANSITIONS) == set(ClaimState)
    assert actual_edges == expected_edges


@pytest.mark.parametrize("state", list(ClaimState))
def test_each_state_rejects_a_representative_illegal_self_edge(state: ClaimState) -> None:
    assert state not in ClaimStateMachine._legal_successors(state)


def test_action_edges_and_state_metadata_are_data() -> None:
    for state in {
        ClaimState.INTIMATED,
        ClaimState.TRIAGED,
        ClaimState.AWAITING_DOCS,
        ClaimState.IN_ASSESSMENT,
        ClaimState.REPORT_RECEIVED,
    }:
        assert ClaimState.VOID in ClaimStateMachine._legal_successors(state)

    for state in ClaimState:
        withdrawal_is_legal = ClaimState.WITHDRAWN in ClaimStateMachine._legal_successors(
            state
        )
        assert withdrawal_is_legal is (
            state
            not in {
                ClaimState.SETTLEMENT,
                ClaimState.SETTLED,
                ClaimState.CLOSED,
                ClaimState.DECLINED,
                ClaimState.WITHDRAWN,
                ClaimState.VOID,
            }
        )

    assert set(STATE_METADATA) == set(ClaimState)
    assert {
        state for state, metadata in STATE_METADATA.items() if metadata.suppresses_activity
    } == {
        ClaimState.DECLINED,
        ClaimState.WITHDRAWN,
        ClaimState.VOID,
        ClaimState.SETTLED,
        ClaimState.CLOSED,
    }
    assert {state for state, metadata in STATE_METADATA.items() if metadata.reopenable} == {
        ClaimState.DECLINED,
        ClaimState.WITHDRAWN,
    }


@pytest.mark.parametrize(
    "path",
    [
        "external.icon.claim_no",
        "external.icon.salvage_no",
        "external.edms.folder_ref",
    ],
)
def test_external_dictionary_paths_restrict_writers(
    client: TestClient, path: str
) -> None:
    definition = CORE_FIELD_DICTIONARY[path]
    assert definition.value_type == "string"
    assert definition.pii_class == "none"
    assert definition.allowed_source_types == frozenset({"projection_readback", "human"})

    claim_id = create_claim(client)
    rejected = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": "REF-1",
                    "value_type": "string",
                    "source_type": "extraction",
                    "verification_state": "extracted",
                }
            ]
        },
        headers=AGENT,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "SOURCE_TYPE_NOT_ALLOWED"

    accepted = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": "REF-1",
                    "value_type": "string",
                    "source_type": "projection_readback",
                    "verification_state": "system_confirmed",
                }
            ]
        },
        headers=SYSTEM,
    )
    assert accepted.status_code == 200


def test_decline_and_substatus_error_contracts(client: TestClient) -> None:
    claim_id = create_claim(client)

    illegal_decline = client.post(
        f"/claims/{claim_id}/decline",
        json={"reason": "below_excess"},
        headers=USER,
    )
    assert illegal_decline.status_code == 409
    assert illegal_decline.json()["code"] == "ILLEGAL_TRANSITION"

    unknown_substatus = client.post(
        f"/claims/{claim_id}/substatus",
        json={"substatus": "UNREGISTERED"},
        headers=USER,
    )
    assert unknown_substatus.status_code == 422
    assert unknown_substatus.json()["code"] == "UNKNOWN_SUBSTATUS"

    missing_claim = client.post(
        "/claims/01JZXY000000000000000NOPE0/transition",
        json={"to": "TRIAGED"},
        headers=AGENT,
    )
    assert missing_claim.status_code == 404
    assert missing_claim.json()["code"] == "CLAIM_NOT_FOUND"
