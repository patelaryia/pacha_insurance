"""PACKET-07 acceptance — PRD-02 §2.4–§2.6 templates, routing, outcomes, guards.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-07_cop_consumers.md §3 (pinned surface, verb table, guard
wiring, ratchets). Failing by design until the packet is built.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest
from sqlalchemy import text

AGENT = {"X-Actor": "agent:cop"}
HUMAN = {"X-Actor": "user:01ARZ3NDEKTSV4RRFFQ69G5FAV"}
ACTOR = "agent:cop"

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

MOTOR_TEMPLATE_IDS = {
    "T-01", "T-02", "T-02b", "T-03", "T-04", "T-05", "T-06", "T-06a",
    "T-06r-broker", "T-06r-client", "T-07", "T-08", "T-08b",
    "T-09", "T-10", "T-11", "T-12", "T-13",
}

ENUM_PATHS = {"salvage.election"}

FIXTURE_REGISTRY = """
templates:
  - id: T-90
    version: 1.0.0
    channel: email
    body_ref: T-90.j2
    required_fields: [parties.insured.name, assessment.estimate_total]
    min_verification: extracted
    locale: en-KE
    status: live
  - id: T-91
    version: 1.0.0
    channel: pdf
    body_ref: T-91.j2
    required_fields: [assessment.agreed_quote]
    min_verification: extracted
    locale: en-KE
    status: live
    calc_slots: {payable: C-08}
  - id: T-92
    version: 1.0.0
    channel: pdf
    body_ref: T-92.j2
    required_fields: []
    min_verification: extracted
    locale: en-KE
    status: live
    calc_slots: {reserve: C-02}
  - id: T-93
    version: 1.0.0
    channel: field_set
    body_ref: null
    required_fields: [reserve.total, vehicle.reg]
    min_verification: extracted
    locale: en-KE
    status: live
  - id: T-94
    version: 1.0.0
    channel: email
    body_ref: T-94.j2
    required_fields: []
    min_verification: extracted
    locale: en-KE
    status: live
  - id: T-95
    version: 1.0.0
    channel: email
    body_ref: T-95.j2
    required_fields: [assessment.estimate_total]
    min_verification: human_verified
    locale: en-KE
    status: live
"""

FIXTURE_BODIES = {
    "T-90.j2": (
        "Dear {{ parties_insured_name }}, the estimate is "
        "{{ assessment_estimate_total }}. REF-T90-STATIC"
    ),
    "T-91.j2": "Amount payable: {{ payable }}. REF-T91-STATIC",
    "T-92.j2": "Reserve total: {{ reserve }}. REF-T92-STATIC",
    "T-94.j2": "This references {{ ghost_variable }}. REF-T94-STATIC",
    "T-95.j2": "Verified estimate {{ assessment_estimate_total }}. REF-T95-STATIC",
}

FIXTURE_RULE_ROUTE_APPROVAL = """
id: R-96
name: fixture_route_approval
applies_to: motor
status: live
inputs: {amount: reserve.total}
when: {">": [{var: amount}, 0]}
outcome: {action: route_approval}
version: 1.0.0
"""


def _make_fixture_pack(tmp_path: pathlib.Path) -> pathlib.Path:
    """Copy the motor pack to version 1.1.0 and add live fixture artifacts."""

    pack = tmp_path / "motor-fixture"
    shutil.copytree(MOTOR_PACK, pack)
    pack_yaml = pack / "pack.yaml"
    pack_yaml.write_text(
        pack_yaml.read_text().replace("version: 1.0.0", "version: 1.1.0")
    )
    (pack / "templates").mkdir(exist_ok=True)
    (pack / "templates" / "registry.yaml").write_text(FIXTURE_REGISTRY)
    for name, body in FIXTURE_BODIES.items():
        (pack / "templates" / name).write_text(body)
    (pack / "rules" / "R-96.yaml").write_text(FIXTURE_RULE_ROUTE_APPROVAL)
    return pack


@pytest.fixture()
def harness(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc7.db")
    app = create_app(url)
    runtime = build_cop_runtime(
        app, pack_paths=[MOTOR_PACK, _make_fixture_pack(tmp_path)]
    )
    return TestClient(app), app, runtime


def _claim(client, pack_version: str = "motor@1.0.0") -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": pack_version}, headers=AGENT
    )
    assert r.status_code == 201
    return r.json()["id"]


def _fixture_claim(client) -> str:
    return _claim(client, "motor@1.1.0")


def _write(client, claim_id: str, values: dict, verification: str = "human_verified"):
    headers = HUMAN if verification == "human_verified" else AGENT
    source = "human" if verification == "human_verified" else "system"
    writes = []
    for path, value in values.items():
        value_type = (
            "enum"
            if path in ENUM_PATHS
            else "money"
            if isinstance(value, int) and not isinstance(value, bool)
            else "bool"
            if isinstance(value, bool)
            else "object"
            if isinstance(value, dict)
            else "string"
        )
        writes.append(
            {
                "path": path,
                "value": value,
                "value_type": value_type,
                "source_type": source,
                "verification_state": verification,
            }
        )
    r = client.patch(
        f"/claims/{claim_id}/fields", json={"writes": writes}, headers=headers
    )
    assert r.status_code == 200, r.text


def _timeline(client, claim_id: str) -> list[dict]:
    r = client.get(f"/claims/{claim_id}/timeline", headers=AGENT)
    assert r.status_code == 200
    return r.json()["events"]


def _events_of(client, claim_id: str, event_type: str) -> list[dict]:
    return [e for e in _timeline(client, claim_id) if e["type"] == event_type]


def _transition(client, claim_id: str, to: str):
    return client.post(
        f"/claims/{claim_id}/transition", json={"to": to}, headers=HUMAN
    )


def _walk(client, claim_id: str, states: list[str]) -> None:
    for state in states:
        r = _transition(client, claim_id, state)
        assert r.status_code == 200, f"{state}: {r.text}"


def _pii_decrypt_count(app) -> int:
    with app.state.engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM audit_ledger WHERE action = 'pii.decrypt'")
        ).scalar()


# --- template registry: motor entries are visible, blocked slots ------------------


def test_motor_template_registry_all_pending_capture(harness):
    _, _, runtime = harness
    registry = runtime.template_registry("motor", "1.0.0")
    assert set(registry.ids()) == MOTOR_TEMPLATE_IDS
    for template_id in MOTOR_TEMPLATE_IDS:
        assert registry.get(template_id).status == "pending_capture", template_id


def test_pending_capture_template_refuses_to_render(harness):
    from cop_runtime.templates import TemplateRenderBlocked

    client, _, runtime = harness
    claim_id = _claim(client)
    with pytest.raises(TemplateRenderBlocked) as excinfo:
        runtime.render("T-07", claim_id, actor=ACTOR)
    assert excinfo.value.reason == "pending_capture"


# --- template engine mechanics (fixture pack) --------------------------------------


def test_render_success_stores_artifact_and_event(harness):
    client, app, runtime = harness
    claim_id = _fixture_claim(client)
    _write(
        client,
        claim_id,
        {"parties.insured.name": "Wanjiku Kamau", "assessment.estimate_total": 250_000_00},
    )
    result = runtime.render("T-90", claim_id, actor=ACTOR)
    assert result.signable is True
    assert result.placeholders_pending == []
    body = app.state.blob_store.get(result.blob_key).decode()
    assert "REF-T90-STATIC" in body
    events = _events_of(client, claim_id, "template.rendered")
    assert len(events) == 1
    assert events[0]["payload"]["template_id"] == "T-90"
    assert events[0]["payload"]["blob_key"] == result.blob_key
    assert "Wanjiku" not in json.dumps(events[0]["payload"])


def test_render_refuses_on_missing_required_field(harness):
    from cop_runtime.templates import TemplateRenderBlocked

    client, _, runtime = harness
    claim_id = _fixture_claim(client)
    _write(client, claim_id, {"parties.insured.name": "Wanjiku Kamau"})
    with pytest.raises(TemplateRenderBlocked) as excinfo:
        runtime.render("T-90", claim_id, actor=ACTOR)
    assert excinfo.value.reason == "missing_fields"
    assert "assessment.estimate_total" in excinfo.value.missing_fields


def test_render_refuses_below_min_verification(harness):
    from cop_runtime.templates import TemplateRenderBlocked

    client, _, runtime = harness
    claim_id = _fixture_claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 250_000_00},
        verification="system_confirmed",
    )
    with pytest.raises(TemplateRenderBlocked) as excinfo:
        runtime.render("T-95", claim_id, actor=ACTOR)
    assert excinfo.value.reason == "under_verified"
    assert "assessment.estimate_total" in excinfo.value.under_verified


def test_blocked_calc_slot_renders_pending_capture_and_refuses_sign(harness):
    client, app, runtime = harness
    claim_id = _fixture_claim(client)
    _write(client, claim_id, {"assessment.agreed_quote": 200_000_00})
    result = runtime.render("T-91", claim_id, actor=ACTOR)
    assert result.signable is False
    assert result.placeholders_pending == ["payable"]
    body = app.state.blob_store.get(result.blob_key).decode()
    assert "PENDING CAPTURE" in body
    assert "REF-T91-STATIC" in body


def test_live_calc_slot_renders_value_and_is_signable(harness):
    client, app, runtime = harness
    claim_id = _fixture_claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 200_000_00,
            "assessment.assessor_fee": 8_000_00,
            "assessment.reinspection_fee": 3_000_00,
        },
    )
    result = runtime.render("T-92", claim_id, actor=ACTOR)
    assert result.signable is True
    assert result.placeholders_pending == []
    body = app.state.blob_store.get(result.blob_key).decode()
    assert "PENDING CAPTURE" not in body
    assert "REF-T92-STATIC" in body


def test_strict_undefined_never_renders_a_blank(harness):
    from cop_runtime.templates import TemplateRenderBlocked

    client, _, runtime = harness
    claim_id = _fixture_claim(client)
    with pytest.raises(TemplateRenderBlocked):
        runtime.render("T-94", claim_id, actor=ACTOR)


def test_field_set_channel_renders_json_artifact(harness):
    client, app, runtime = harness
    claim_id = _fixture_claim(client)
    _write(
        client,
        claim_id,
        {"reserve.total": 211_000_00, "vehicle.reg": "KDA 123B"},
    )
    result = runtime.render("T-93", claim_id, actor=ACTOR)
    assert result.channel == "field_set"
    payload = json.loads(app.state.blob_store.get(result.blob_key).decode())
    assert payload == {"reserve.total": 211_000_00, "vehicle.reg": "KDA 123B"}


# --- routing (§2.5 verbatim, inclusive bounds) --------------------------------------


def test_routing_band_boundaries_inclusive(harness):
    _, _, runtime = harness
    matrix = runtime.authority_matrix("motor", "1.0.0")
    cases = [
        (0, "asst_claims_manager"),
        (100_000_00, "asst_claims_manager"),   # inclusive boundary
        (100_000_01, "claims_manager"),
        (700_000_00, "claims_manager"),
        (700_000_01, "gm"),
        (1_500_000_00, "gm"),
        (4_000_000_00, "md"),
        (4_000_000_01, "chairman"),
    ]
    for amount, role in cases:
        result = matrix.route(amount)
        assert result.role == role, amount


def test_chairman_band_carries_t03_side_effect(harness):
    _, _, runtime = harness
    result = runtime.authority_matrix("motor", "1.0.0").route(5_000_000_00)
    assert result.role == "chairman"
    assert result.side_effects == ["render T-03"]


def test_negative_amount_refuses(harness):
    _, _, runtime = harness
    with pytest.raises(ValueError):
        runtime.authority_matrix("motor", "1.0.0").route(-1)


def test_matrix_with_gap_or_overlap_fails_load(harness, tmp_path):
    from cop_runtime import PackLoadError

    _, _, runtime = harness
    for suffix, bad_matrix in (
        ("gap", "- {max: 100_000_00, role: a}\n- {min_broken: true, max: null, role: b}\n"),
        ("noterminal", "- {max: 100_000_00, role: a}\n- {max: 700_000_00, role: b}\n"),
    ):
        bad = tmp_path / f"motor-{suffix}"
        shutil.copytree(MOTOR_PACK, bad)
        (bad / "pack.yaml").write_text(
            (bad / "pack.yaml").read_text().replace("1.0.0", f"1.9.{1 if suffix == 'gap' else 2}")
        )
        (bad / "routing" / "authority_matrix.yaml").write_text(bad_matrix)
        with pytest.raises(PackLoadError):
            runtime.load_pack(bad)


def test_route_for_claim_uses_reserve_fallback(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"reserve.total": 100_000_00})
    assert runtime.route_for_claim(claim_id, actor=ACTOR).role == "asst_claims_manager"


def test_route_for_claim_without_amount_refuses(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    with pytest.raises(LookupError):
        runtime.route_for_claim(claim_id, actor=ACTOR)


# --- outcome execution ---------------------------------------------------------------


def _fired(runtime, client, claim_id, rule_id, values):
    _write(client, claim_id, values)
    result = runtime.evaluate(rule_id, claim_id, actor=ACTOR)
    assert result.fired is True, result
    return result


def test_set_field_outcome_writes_rule_provenance(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(
        runtime,
        client,
        claim_id,
        "R-05",
        {
            "assessment.agreed_quote": 600_000_00,
            "assessment.pav": 1_000_000_00,
            "policy.sum_insured": 1_200_000_00,
        },
    )
    runtime.execute_outcome(result, actor=ACTOR)
    r = client.get(f"/claims/{claim_id}", headers=AGENT)
    field = r.json()["fields"]["assessment.write_off_indicated"]
    assert field["value"] is True
    assert field["source_type"] == "rule"
    assert field["verification_state"] == "system_confirmed"
    assert field["source_ref"]["rule_id"] == "R-05"
    assert field["source_ref"]["rule_run_id"]


def test_value_map_outcome_selects_variant(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(runtime, client, claim_id, "R-15", {"salvage.election": "retain"})
    runtime.execute_outcome(result, actor=ACTOR)
    r = client.get(f"/claims/{claim_id}", headers=AGENT)
    assert r.json()["fields"]["settlement.c07_variant"]["value"] == "retained"


def test_set_field_never_supersedes_human_verified(harness):
    from claim_core import HumanOverrideProtected

    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(
        runtime,
        client,
        claim_id,
        "R-05",
        {
            "assessment.agreed_quote": 600_000_00,
            "assessment.pav": 1_000_000_00,
            "policy.sum_insured": 1_200_000_00,
        },
    )
    _write(client, claim_id, {"assessment.write_off_indicated": False})
    with pytest.raises(HumanOverrideProtected):
        runtime.execute_outcome(result, actor=ACTOR)


def test_route_review_creates_ex_gratia_item_event(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = runtime.evaluate("R-03", claim_id, actor=ACTOR)
    runtime.execute_outcome(result, actor=ACTOR)
    events = _events_of(client, claim_id, "review.created")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["type"] == "EX_GRATIA"
    assert payload["route"] == "EX_GRATIA_REVIEW"
    assert payload["role"] == "claims_manager"
    assert payload["rule_run_id"]


def test_propose_decline_creates_draft_release_and_no_transition(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(
        runtime,
        client,
        claim_id,
        "R-02",
        {"assessment.estimate_total": 20_000_00, "policy.excess_amount": 30_000_00},
    )
    runtime.execute_outcome(result, actor=ACTOR)
    events = _events_of(client, claim_id, "review.created")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["type"] == "DRAFT_RELEASE"
    assert payload["subtype"] == "decline_draft"
    assert payload["draft_template"] == "T-07"
    r = client.get(f"/claims/{claim_id}", headers=AGENT)
    assert r.json()["status"] == "INTIMATED"


def test_emit_event_with_pending_template_is_visible_exception(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(runtime, client, claim_id, "R-12", {"reserve.total": 4_000_000_01})
    runtime.execute_outcome(result, actor=ACTOR)
    events = _events_of(client, claim_id, "review.created")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["type"] == "EXCEPTION"
    assert payload["subtype"] == "template_pending_capture"
    assert payload["template_id"] == "T-03"


def test_block_outcome_has_no_side_effects(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    result = _fired(
        runtime,
        client,
        claim_id,
        "R-13",
        {"salvage.logbook_held": True, "salvage.keys_held": False},
    )
    before = len(_timeline(client, claim_id))
    outcome = runtime.execute_outcome(result, actor=ACTOR)
    assert outcome.action == "block"
    assert len(_timeline(client, claim_id)) == before


def test_execute_outcome_refuses_unfired_result(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 50_000_00, "policy.excess_amount": 30_000_00},
    )
    result = runtime.evaluate("R-02", claim_id, actor=ACTOR)
    assert result.fired is False
    with pytest.raises(ValueError):
        runtime.execute_outcome(result, actor=ACTOR)


def test_route_approval_execution_is_deferred(harness):
    client, _, runtime = harness
    claim_id = _fixture_claim(client)
    result = _fired(runtime, client, claim_id, "R-96", {"reserve.total": 1_00})
    with pytest.raises(NotImplementedError):
        runtime.execute_outcome(result, actor=ACTOR)


def test_loader_rejects_seventh_outcome_verb(harness, tmp_path):
    from cop_runtime import PackLoadError

    _, _, runtime = harness
    bad = tmp_path / "motor-verb"
    shutil.copytree(MOTOR_PACK, bad)
    (bad / "pack.yaml").write_text(
        (bad / "pack.yaml").read_text().replace("1.0.0", "1.9.3")
    )
    (bad / "rules" / "R-97.yaml").write_text(
        "id: R-97\nname: bad_verb\napplies_to: motor\nstatus: live\n"
        "inputs: {amount: reserve.total}\nwhen: {'>': [{var: amount}, 0]}\n"
        "outcome: {action: pay_immediately}\nversion: 1.0.0\n"
    )
    with pytest.raises(PackLoadError):
        runtime.load_pack(bad)


# --- FSM guard wiring (register #24) -------------------------------------------------


def test_write_off_edge_requires_r05_fired(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _walk(client, claim_id, ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"])

    # No R-05 inputs at all: blocked, never a silent pass.
    r = _transition(client, claim_id, "WRITE_OFF")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "TRANSITION_GUARD_BLOCKED"

    # Inputs present but rule not fired (exactly 50%): still blocked.
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 500_000_00,
            "assessment.pav": 1_000_000_00,
            "policy.sum_insured": 1_200_000_00,
        },
    )
    assert _transition(client, claim_id, "WRITE_OFF").status_code == 409

    # One cent over: transition proceeds and the wired guard is not "pending".
    _write(client, claim_id, {"assessment.agreed_quote": 500_000_01})
    r = _transition(client, claim_id, "WRITE_OFF")
    assert r.status_code == 200, r.text
    transitions = _events_of(client, claim_id, "claim.status_changed")
    write_off_events = [e for e in transitions if e["payload"].get("to") == "WRITE_OFF"]
    assert write_off_events
    assert "R-05 true" not in write_off_events[-1]["payload"].get("guards_pending", [])


def test_settlement_edge_hard_blocked_while_r14_uncaptured(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _walk(client, claim_id, ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"])
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 600_000_00,
            "assessment.pav": 1_000_000_00,
            "policy.sum_insured": 1_200_000_00,
            "salvage.logbook_held": True,
            "salvage.keys_held": True,
        },
    )
    _walk(
        client,
        claim_id,
        ["WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION", "SURRENDER_CHECKLIST"],
    )
    r = _transition(client, claim_id, "SETTLEMENT")
    assert r.status_code == 409
    blocked_on = " ".join(r.json().get("blocked_on", []))
    assert "R-14" in blocked_on


def test_reinspection_edge_requires_r08(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _walk(client, claim_id, ["TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED"])
    _write(client, claim_id, {"external.icon.claim_no": "ICON-42"})
    _walk(
        client,
        claim_id,
        ["REGISTERED", "RESERVED", "PACK_READY", "IN_APPROVAL", "APPROVED", "IN_REPAIR"],
    )

    _write(
        client,
        claim_id,
        {"assessment.agreed_quote": 40_000_00, "assessment.parts_replaced": False},
    )
    assert _transition(client, claim_id, "REINSPECTION").status_code == 409

    _write(client, claim_id, {"assessment.parts_replaced": True})
    assert _transition(client, claim_id, "REINSPECTION").status_code == 200


# --- PACKET-06 ratchets ---------------------------------------------------------------


def test_rule_evaluation_never_decrypts_unbound_pii(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"parties.insured.national_id": "12345678", "reserve.total": 4_000_000_01},
    )
    baseline = _pii_decrypt_count(app)
    runtime.evaluate("R-12", claim_id, actor=ACTOR)
    assert _pii_decrypt_count(app) == baseline


def test_rule_evaluated_events_carry_no_fake_correlation(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    runtime.evaluate("R-03", claim_id, actor=ACTOR)
    events = _events_of(client, claim_id, "rule.evaluated")
    assert events
    assert all(e["correlation_id"] is None for e in events)


def test_selective_hydration_paths_parameter(harness):
    client, app, _ = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"parties.insured.national_id": "12345678", "reserve.total": 100_000_00},
    )
    baseline = _pii_decrypt_count(app)
    _claim_obj, fields, _blocked = app.state.claim_service.hydrate_claim(
        claim_id, ACTOR, paths=["reserve.total"]
    )
    assert set(fields) == {"reserve.total"}
    assert _pii_decrypt_count(app) == baseline
