"""Builder-owned regression pins for PACKET-14 review findings."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PACKET_PATH = Path(__file__).resolve().parents[1] / "acceptance" / "test_packet_14_intake_flow.py"
SPEC = importlib.util.spec_from_file_location("packet14_acceptance_harness", PACKET_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("PACKET-14 acceptance harness could not be loaded")
packet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packet
SPEC.loader.exec_module(packet)


def test_no_estimate_keeps_r02_blocked_and_completes_triage(tmp_path):
    env = packet._build(tmp_path, "no-estimate", model=packet._clean_model())
    packet._set_level(env.app, "intake.claim_creation", "L3")
    packet._start_clean_intimation(env)
    claim_id = packet._claims(env.app)[0]["id"]
    packet._approve_ack(env, claim_id)

    packet._resolve_coverage(env, claim_id)

    claim = env.client.get(
        f"/claims/{claim_id}", headers=packet._h(packet.OFFICER_A)
    ).json()
    assert claim["status"] == "TRIAGED"
    runs = packet._rows(
        env.app,
        "SELECT status, missing_inputs FROM rule_runs "
        "WHERE claim_id = :claim_id AND rule_id = 'R-02'",
        claim_id=claim_id,
    )
    assert len(runs) == 1 and runs[0]["status"] == "blocked_on_inputs"
    assert packet._items(
        env.app,
        claim_id=claim_id,
        type="EXCEPTION",
        subtype="triage_blocked_on_inputs",
    ) == []


def test_rejected_claim_creation_completes_as_visible_no_op(tmp_path):
    env = packet._build(tmp_path, "creation-rejected", model=packet._clean_model())
    packet._start_clean_intimation(env)
    staged = next(
        item
        for item in packet._items(env.app, type="DRAFT_RELEASE")
        if item["payload"].get("capability_id") == "intake.claim_creation"
    )

    response = packet._resolve(
        env,
        staged["id"],
        packet.OFFICER_A,
        action="reject",
        schema_version="DRAFT_RELEASE@1",
        payload={
            "capability_id": "intake.claim_creation",
            "diff": packet._diff(),
            "reason": "intimation was opened in error",
        },
    )
    assert response.status_code == 200, response.text
    packet._advance(env)

    run = packet._rows(
        env.app,
        "SELECT status, steps, error FROM agent_runs ORDER BY started_at, id",
    )[0]
    assert run["status"] == "completed"
    assert run["error"] in {None, "null"}
    assert run["steps"][0]["outcome"]["result"] == "no_op"
    assert packet._claims(env.app) == []
    assert packet._items(
        env.app, type="EXCEPTION", subtype="agent_run_failed"
    ) == []


def test_rejected_coverage_card_reissues_the_keying_path(tmp_path):
    env = packet._build(tmp_path, "coverage-rejected", model=packet._clean_model())
    packet._set_level(env.app, "intake.claim_creation", "L3")
    packet._start_clean_intimation(env)
    claim_id = packet._claims(env.app)[0]["id"]
    packet._approve_ack(env, claim_id)
    first = packet._open_item(
        env, claim_id, type_="FIELD_VERIFY", subtype="coverage_manual"
    )

    response = packet._resolve(
        env,
        first["id"],
        packet.OFFICER_A,
        action="reject",
        schema_version="FIELD_VERIFY_COVERAGE@1",
        payload={
            "capability_id": "triage.coverage_check",
            "diff": packet._diff(),
            "reason": "policy record needs to be selected again",
            "fields": {"policy.premium_paid": True},
        },
    )
    assert response.status_code == 200, response.text
    packet._advance(env)

    retry = packet._open_item(
        env, claim_id, type_="FIELD_VERIFY", subtype="coverage_manual"
    )
    assert retry["id"] != first["id"]
    assert retry["payload"]["retry_of"] == first["id"]
    assert packet._items(
        env.app,
        claim_id=claim_id,
        type="EXCEPTION",
        subtype="coverage_manual_rejected",
    ) == []


def test_captured_t07_declines_then_stages_the_letter(tmp_path, monkeypatch):
    env = packet._build(tmp_path, "captured-t07", model=packet._triage_model())
    # AR-3 correctly queues non-urgent communications outside 08:00–18:00
    # EAT. Pin this staging regression to Tuesday 10:00 EAT so it does not
    # change outcome with the CI runner's wall clock.
    monkeypatch.setattr(
        env.app.state,
        "clock",
        lambda: datetime(2026, 7, 21, 7, 0, tzinfo=UTC),
    )
    claim_id = packet._drive_to_coverage_card(env)
    packet._resolve_coverage(env, claim_id)
    decline = packet._open_item(
        env, claim_id, type_="DRAFT_RELEASE", subtype="decline_draft"
    )
    captured = SimpleNamespace(status="live")
    registry = SimpleNamespace(get=lambda _template_id: captured)
    rendered = SimpleNamespace(
        blob_key="templates/T-07/test.pdf",
        signable=True,
        placeholders_pending=False,
    )
    monkeypatch.setattr(
        env.app.state.cop_runtime,
        "template_registry",
        lambda _pack_id, _version: registry,
    )
    monkeypatch.setattr(
        env.app.state.cop_runtime,
        "render",
        lambda _template_id, _claim_id, _actor: rendered,
    )

    response = packet._resolve(
        env,
        decline["id"],
        packet.OFFICER_A,
        action="approve",
        schema_version="DRAFT_RELEASE@1",
        payload={"capability_id": "triage.decline_draft", "diff": packet._diff()},
    )
    assert response.status_code == 200, response.text
    packet._advance(env)

    claim = env.client.get(
        f"/claims/{claim_id}", headers=packet._h(packet.OFFICER_A)
    ).json()
    assert claim["status"] == "DECLINED"
    letter = packet._open_item(env, claim_id, type_="DRAFT_RELEASE", template_id="T-07")
    assert letter["payload"]["capability_id"] == "triage.decline_draft"
