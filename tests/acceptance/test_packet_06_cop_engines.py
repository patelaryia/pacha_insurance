"""PACKET-06 acceptance — PRD-02 §2.2–§2.3 rule runtime + calc registry.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-06_cop_engines.md §3 (runtime surface, RuleResult/CalcResult
shapes, rule_runs/calc_runs DDL, motor pack rule register). Failing by design
until the packet is built.
"""
from __future__ import annotations

import os
import pathlib
import shutil

import pytest
from sqlalchemy import text

AGENT = {"X-Actor": "agent:cop"}
HUMAN = {"X-Actor": "user:01ARZ3NDEKTSV4RRFFQ69G5FAV"}  # user: + 26-char ULID (D-3)
ACTOR = "agent:cop"

# Paths whose dictionary value_type is not inferable from the JSON value.
ENUM_PATHS = {"salvage.election"}

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

ALL_SLOTS = [f"R-{n:02d}" for n in range(1, 17)]
BLOCKED_SLOTS = {"R-01", "R-04", "R-06", "R-07", "R-10", "R-11", "R-14", "R-16"}
ALL_CALCS = ["C-01", "C-02", "C-03", "C-04", "C-05", "C-06", "C-07", "C-08"]
BLOCKED_CALCS = {"C-04", "C-07", "C-08"}


@pytest.fixture()
def harness(tmp_path):
    from cop_runtime import build_cop_runtime
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc6.db")
    app = create_app(url)
    runtime = build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    return TestClient(app), app, runtime


def _claim(client, pack_version: str = "motor@1.0.0") -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": pack_version}, headers=AGENT
    )
    assert r.status_code == 201
    return r.json()["id"]


def _write(client, claim_id: str, values: dict) -> None:
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
                "source_type": "human",
                "verification_state": "human_verified",
            }
        )
    r = client.patch(
        f"/claims/{claim_id}/fields", json={"writes": writes}, headers=HUMAN
    )
    assert r.status_code == 200, r.text


def _rule_runs(app, claim_id: str) -> list[dict]:
    with app.state.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rule_id, rule_version, pack_id, pack_version, status, "
                "fired, actor FROM rule_runs WHERE claim_id = :c"
            ),
            {"c": claim_id},
        )
        return [dict(r._mapping) for r in rows]


def _calc_runs(app, claim_id: str) -> list[dict]:
    with app.state.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT calc_id, version, status, claim_id, ts "
                "FROM calc_runs WHERE claim_id = :c"
            ),
            {"c": claim_id},
        )
        return [dict(r._mapping) for r in rows]


def _timeline_types(client, claim_id: str) -> list[str]:
    r = client.get(f"/claims/{claim_id}/timeline", headers=AGENT)
    assert r.status_code == 200
    return [e["type"] for e in r.json()["events"]]


# --- registries -----------------------------------------------------------------


def test_all_sixteen_rule_slots_registered(harness):
    _, _, runtime = harness
    registry = runtime.rule_registry("motor", "1.0.0")
    assert sorted(registry.ids()) == sorted(ALL_SLOTS)


def test_blocked_rule_slots_visible(harness):
    _, _, runtime = harness
    registry = runtime.rule_registry("motor", "1.0.0")
    for rule_id in ALL_SLOTS:
        expected = (
            "blocked_on_inputs" if rule_id in BLOCKED_SLOTS else "live"
        )
        assert registry.get(rule_id).status == expected, rule_id


def test_all_calc_slots_registered_with_blocked_status(harness):
    _, _, runtime = harness
    registry = runtime.calc_registry("motor", "1.0.0")
    assert sorted(registry.ids()) == sorted(ALL_CALCS)
    for calc_id in BLOCKED_CALCS:
        assert registry.get(calc_id).status == "blocked_on_inputs", calc_id


def test_runtime_exposed_on_app_state(harness):
    _, app, runtime = harness
    assert app.state.cop_runtime is runtime


# --- R-02: below-excess, fires at equality (§2.6 boundary) -----------------------


def test_r02_fires_when_estimate_equals_excess_exactly(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 30_000_00, "policy.excess_amount": 30_000_00},
    )
    res = runtime.evaluate("R-02", claim_id, actor=ACTOR)
    assert res.status == "evaluated"
    assert res.fired is True
    assert res.outcome["action"] == "propose_decline"
    assert res.outcome["draft_template"] == "T-07"
    assert res.outcome["exception"]["route"] == "EX_GRATIA_REVIEW"
    assert res.outcome["exception"]["role"] == "claims_manager"
    assert res.inputs_snapshot == {"estimate": 30_000_00, "excess": 30_000_00}
    assert res.rule_version == "1.0.0"


def test_r02_not_fired_above_excess(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 30_000_01, "policy.excess_amount": 30_000_00},
    )
    res = runtime.evaluate("R-02", claim_id, actor=ACTOR)
    assert res.fired is False
    assert res.outcome is None


def test_r02_missing_input_blocked_never_silent_false(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"policy.excess_amount": 30_000_00})
    res = runtime.evaluate("R-02", claim_id, actor=ACTOR)
    assert res.status == "blocked_on_inputs"
    assert res.fired is None
    assert "assessment.estimate_total" in res.missing_inputs
    runs = _rule_runs(app, claim_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "blocked_on_inputs"
    assert runs[0]["fired"] is None
    assert "rule.evaluated" in _timeline_types(client, claim_id)


# --- R-03: pure route_review, no auto path ---------------------------------------


def test_r03_pure_route_review(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    res = runtime.evaluate("R-03", claim_id, actor=ACTOR)
    assert res.status == "evaluated"
    assert res.fired is True
    assert res.outcome["action"] == "route_review"
    assert res.outcome["route"] == "EX_GRATIA_REVIEW"
    assert res.outcome["role"] == "claims_manager"


# --- R-05: strictly greater than 50% (§2.6 boundary) ------------------------------


def _write_r05(client, claim_id, quote: int) -> None:
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": quote,
            "assessment.pav": 1_000_000_00,
            "policy.sum_insured": 1_200_000_00,
        },
    )


def test_r05_quote_at_exactly_half_pav_does_not_fire(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write_r05(client, claim_id, 500_000_00)  # exactly 50.0% of min(pav, si)
    res = runtime.evaluate("R-05", claim_id, actor=ACTOR)
    assert res.status == "evaluated"
    assert res.fired is False


def test_r05_one_cent_over_half_fires_write_off(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write_r05(client, claim_id, 500_000_01)
    res = runtime.evaluate("R-05", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["action"] == "set_field"
    assert res.outcome["path"] == "assessment.write_off_indicated"


def test_r05_uses_min_of_pav_and_sum_insured(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 500_000_01,
            "assessment.pav": 1_200_000_00,
            "policy.sum_insured": 1_000_000_00,  # min is sum_insured here
        },
    )
    res = runtime.evaluate("R-05", claim_id, actor=ACTOR)
    assert res.fired is True


# --- R-08: 50,000 desk / 50,001 physical (guide §4 boundary) ----------------------


def test_r08_at_fifty_thousand_exactly_not_fired(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.agreed_quote": 50_000_00, "assessment.parts_replaced": False},
    )
    res = runtime.evaluate("R-08", claim_id, actor=ACTOR)
    assert res.status == "evaluated"
    assert res.fired is False


def test_r08_one_cent_over_fires_physical(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.agreed_quote": 50_000_01, "assessment.parts_replaced": False},
    )
    res = runtime.evaluate("R-08", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["action"] == "set_field"


def test_r08_parts_replaced_fires_regardless_of_quote(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.agreed_quote": 10_000_00, "assessment.parts_replaced": True},
    )
    res = runtime.evaluate("R-08", claim_id, actor=ACTOR)
    assert res.fired is True


# --- R-09 / R-13: block verbs ------------------------------------------------------


def test_r09_same_assessor_violation_blocks(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.validation_assessor_id": "AS-7",
            "assessment.initial_assessor_id": "AS-7",
        },
    )
    res = runtime.evaluate("R-09", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["action"] == "block"


def test_r09_distinct_assessors_pass(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.validation_assessor_id": "AS-7",
            "assessment.initial_assessor_id": "AS-2",
        },
    )
    res = runtime.evaluate("R-09", claim_id, actor=ACTOR)
    assert res.fired is False


def test_r13_blocks_unless_logbook_and_keys_held(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"salvage.logbook_held": True, "salvage.keys_held": False},
    )
    res = runtime.evaluate("R-13", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["action"] == "block"


def test_r13_passes_when_both_held(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"salvage.logbook_held": True, "salvage.keys_held": True},
    )
    res = runtime.evaluate("R-13", claim_id, actor=ACTOR)
    assert res.fired is False


# --- R-12: 4M boundary, routing-amount fallback ------------------------------------


def test_r12_at_exactly_four_million_not_fired(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"reserve.total": 4_000_000_00})
    res = runtime.evaluate("R-12", claim_id, actor=ACTOR)
    assert res.status == "evaluated"
    assert res.fired is False


def test_r12_one_cent_over_fires_t03_alert(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"reserve.total": 4_000_000_01})
    res = runtime.evaluate("R-12", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["draft_template"] == "T-03"


def test_routing_amount_falls_back_to_reserve_total_while_c08_blocked(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"reserve.total": 250_000_00})
    assert runtime.routing_amount(claim_id, actor=ACTOR) == 250_000_00


# --- R-15: variant selection outcome data ------------------------------------------


def test_r15_retain_selects_variant_outcome(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"salvage.election": "retain"})
    res = runtime.evaluate("R-15", claim_id, actor=ACTOR)
    assert res.fired is True
    assert res.outcome["action"] == "set_field"
    assert res.outcome["path"] == "settlement.c07_variant"
    assert res.outcome["value_map"] == {
        "retain": "retained",
        "surrender": "surrendered",
    }


# --- blocked slots evaluate visibly, never silently --------------------------------


def test_blocked_rule_evaluation_recorded_and_visible(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    res = runtime.evaluate("R-06", claim_id, actor=ACTOR)
    assert res.status == "blocked_on_inputs"
    assert res.fired is None
    runs = _rule_runs(app, claim_id)
    assert runs[0]["rule_id"] == "R-06"
    assert runs[0]["status"] == "blocked_on_inputs"
    assert "rule.evaluated" in _timeline_types(client, claim_id)


def test_unknown_rule_id_refuses(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    with pytest.raises(LookupError):
        runtime.evaluate("R-99", claim_id, actor=ACTOR)


# --- every evaluation writes rule_runs + event -------------------------------------


def test_every_evaluation_writes_rule_run_and_event(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 10_000_00, "policy.excess_amount": 30_000_00},
    )
    runtime.evaluate("R-02", claim_id, actor=ACTOR)
    runtime.evaluate("R-02", claim_id, actor=ACTOR)
    runs = _rule_runs(app, claim_id)
    assert len(runs) == 2
    assert all(r["rule_version"] == "1.0.0" for r in runs)
    assert all(r["pack_id"] == "motor" for r in runs)
    assert all(r["pack_version"] == "1.0.0" for r in runs)
    assert all(r["actor"] == ACTOR for r in runs)
    assert _timeline_types(client, claim_id).count("rule.evaluated") == 2


# --- pack versioning: data-only change, pinned versions ----------------------------


def _bump_pack(tmp_path) -> pathlib.Path:
    """Copy the motor pack, bump to 1.0.1, raise the R-08 threshold. Data only."""
    bumped = tmp_path / "motor-1.0.1"
    shutil.copytree(MOTOR_PACK, bumped)
    pack_yaml = bumped / "pack.yaml"
    pack_yaml.write_text(
        pack_yaml.read_text().replace("version: 1.0.0", "version: 1.0.1")
    )
    for rule_file in (bumped / "rules").glob("*.yaml"):
        body = rule_file.read_text()
        if "id: R-08" in body:
            rule_file.write_text(body.replace("50_000_00", "60_000_00"))
            break
    else:
        raise AssertionError("R-08 rule file not found in pack copy")
    return bumped


def test_rule_change_is_pack_version_bump_zero_code_release(harness, tmp_path):
    client, app, runtime = harness
    runtime.load_pack(_bump_pack(tmp_path))

    old = _claim(client, "motor@1.0.0")
    new = _claim(client, "motor@1.0.1")
    for claim_id in (old, new):
        _write(
            client,
            claim_id,
            {"assessment.agreed_quote": 55_000_00, "assessment.parts_replaced": False},
        )

    assert runtime.evaluate("R-08", old, actor=ACTOR).fired is True
    assert runtime.evaluate("R-08", new, actor=ACTOR).fired is False

    assert _rule_runs(app, old)[0]["pack_version"] == "1.0.0"
    assert _rule_runs(app, new)[0]["pack_version"] == "1.0.1"


def test_claim_pinned_to_unloaded_pack_version_refuses(harness):
    client, _, runtime = harness
    claim_id = _claim(client, "motor@9.9.9")
    with pytest.raises(LookupError, match="PACK_VERSION_NOT_LOADED"):
        runtime.evaluate("R-02", claim_id, actor=ACTOR)


def test_effective_from_is_documentation_metadata_only(harness):
    # R-02 carries effective_from 2026-08-01 (PRD §2.2 YAML verbatim); it must
    # evaluate regardless — pinning is the sole versioning authority.
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 1_00, "policy.excess_amount": 2_00},
    )
    assert runtime.evaluate("R-02", claim_id, actor=ACTOR).fired is True


# --- pack loader failure modes ------------------------------------------------------


def test_loader_rejects_rule_with_unresolvable_input_path(harness, tmp_path):
    from cop_runtime import PackLoadError

    _, _, runtime = harness
    bad = tmp_path / "motor-bad"
    shutil.copytree(MOTOR_PACK, bad)
    (bad / "pack.yaml").write_text(
        (bad / "pack.yaml").read_text().replace("version: 1.0.0", "version: 1.0.2")
    )
    (bad / "rules" / "R-99.yaml").write_text(
        "id: R-99\nname: bad_path\napplies_to: motor\nstatus: live\n"
        "inputs: {x: no.such.path}\nwhen: {'>': [{var: x}, 1]}\n"
        "outcome: {action: block}\nversion: 1.0.0\n"
    )
    with pytest.raises(PackLoadError):
        runtime.load_pack(bad)


def test_loader_rejects_calc_sandbox_violation(harness, tmp_path):
    from cop_runtime import PackLoadError

    _, _, runtime = harness
    bad = tmp_path / "motor-io"
    shutil.copytree(MOTOR_PACK, bad)
    (bad / "pack.yaml").write_text(
        (bad / "pack.yaml").read_text().replace("version: 1.0.0", "version: 1.0.3")
    )
    calcs = bad / "calcs" / "calcs.py"
    calcs.write_text("import os\n" + calcs.read_text())
    with pytest.raises(PackLoadError):
        runtime.load_pack(bad)


def test_loader_rejects_duplicate_pack_version(harness):
    from cop_runtime import PackLoadError

    _, _, runtime = harness
    with pytest.raises(PackLoadError):
        runtime.load_pack(MOTOR_PACK)


# --- calcs -------------------------------------------------------------------------


def test_c01_excess_clamp_boundaries(harness):
    client, _, runtime = harness
    cases = [
        (400_000_00, 15_000_00),    # 2.5% = 10,000_00 → clamped to floor
        (600_000_00, 15_000_00),    # 2.5% = 15,000_00 → exactly the floor
        (2_000_000_00, 50_000_00),  # interior: 2.5% exact
        (4_000_000_00, 100_000_00), # 2.5% = 100,000_00 → exactly the cap
        (6_000_000_00, 100_000_00), # 2.5% = 150,000_00 → clamped to cap
    ]
    for sum_insured, expected in cases:
        claim_id = _claim(client)
        _write(client, claim_id, {"policy.sum_insured": sum_insured})
        res = runtime.execute_calc("C-01", claim_id, actor=ACTOR)
        assert res.status == "executed"
        assert res.output == expected, sum_insured
        assert isinstance(res.output, int)
        assert not isinstance(res.output, bool)


def test_c02_reserve_sum_and_calc_run_row(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 200_000_00,
            "assessment.assessor_fee": 8_000_00,
            "assessment.reinspection_fee": 3_000_00,
        },
    )
    res = runtime.execute_calc("C-02", claim_id, actor=ACTOR)
    assert res.output == 211_000_00
    runs = _calc_runs(app, claim_id)
    assert len(runs) == 1
    assert runs[0]["calc_id"] == "C-02"
    assert runs[0]["version"] == "1.0.0"
    assert runs[0]["status"] == "executed"
    assert "calc.executed" in _timeline_types(client, claim_id)


def test_c03_breakdown_lines_sum_to_c02(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 200_000_00,
            "assessment.assessor_fee": 8_000_00,
            "assessment.reinspection_fee": 3_000_00,
            "assessment.garage_party_id": "P-GARAGE",
            "assessment.assessor_party_id": "P-ASSESSOR",
            "assessment.supplier_lines": {
                "lines": [
                    {"payee_party_id": "P-SUP1", "amount": 40_000_00},
                    {"payee_party_id": "P-SUP2", "amount": 25_000_00},
                ]
            },
        },
    )
    reserve = runtime.execute_calc("C-02", claim_id, actor=ACTOR)
    res = runtime.execute_calc("C-03", claim_id, actor=ACTOR)
    assert res.status == "executed"
    lines = res.output
    assert sum(line["amount"] for line in lines) == reserve.output
    categories = {line["category"] for line in lines}
    assert {"garage_residual", "assessor", "reinspection_residual"} <= categories
    for line in lines:
        assert set(line) >= {"category", "payee_party_id", "amount", "parent_reserve_id"}
        assert isinstance(line["amount"], int)


def test_c03_without_prior_c02_run_is_blocked(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_quote": 200_000_00,
            "assessment.assessor_fee": 8_000_00,
            "assessment.reinspection_fee": 3_000_00,
            "assessment.garage_party_id": "P-GARAGE",
            "assessment.assessor_party_id": "P-ASSESSOR",
            "assessment.supplier_lines": {"lines": []},
        },
    )
    res = runtime.execute_calc("C-03", claim_id, actor=ACTOR)
    assert res.status == "blocked_on_inputs"


def test_c05_savings(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.estimate_total": 250_000_00,
            "assessment.agreed_quote": 200_000_00,
        },
    )
    res = runtime.execute_calc("C-05", claim_id, actor=ACTOR)
    assert res.status == "executed"
    savings = res.output if isinstance(res.output, int) else res.output["savings"]
    assert savings == 50_000_00


def test_c06_write_off_reserve(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {
            "assessment.agreed_value": 900_000_00,
            "assessment.assessor_fee": 8_000_00,
            "assessment.towing_fee": 15_000_00,
        },
    )
    res = runtime.execute_calc("C-06", claim_id, actor=ACTOR)
    assert res.output == 923_000_00


def test_c07_and_c08_blocked_never_produce_a_number(harness):
    client, app, runtime = harness
    claim_id = _claim(client)
    for calc_id in ("C-07", "C-08"):
        res = runtime.execute_calc(calc_id, claim_id, actor=ACTOR)
        assert res.status == "blocked_on_inputs"
        assert res.output is None
    runs = _calc_runs(app, claim_id)
    assert {r["calc_id"] for r in runs} == {"C-07", "C-08"}
    assert all(r["status"] == "blocked_on_inputs" for r in runs)


def test_calc_missing_claim_input_is_blocked(harness):
    client, _, runtime = harness
    claim_id = _claim(client)  # no policy.sum_insured written
    res = runtime.execute_calc("C-01", claim_id, actor=ACTOR)
    assert res.status == "blocked_on_inputs"
    assert "policy.sum_insured" in res.missing_inputs


def test_unknown_calc_id_refuses(harness):
    client, _, runtime = harness
    claim_id = _claim(client)
    with pytest.raises(LookupError):
        runtime.execute_calc("C-99", claim_id, actor=ACTOR)
