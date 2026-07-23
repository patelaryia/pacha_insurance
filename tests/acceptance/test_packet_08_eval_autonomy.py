"""PACKET-08 acceptance — PRD-03 §3.2–§3.4/§3.6 deterministic eval + autonomy.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-08_eval_autonomy.md §3 (pinned surface, grader logic,
counter semantics, constitution). Failing by design until the packet is built.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

AGENT = {"X-Actor": "agent:cop"}
HUMAN = {"X-Actor": "user:01ARZ3NDEKTSV4RRFFQ69G5FAV"}
ACTOR = "agent:eval"
CM_SIGN = {"actor": "user:01BX5ZZKBKACTAV9WEVGEMMVRZ", "role": "claims_manager"}
MD_SIGN = {"actor": "user:01BX5ZZKBKACTAV9WEVGEMMVS0", "role": "md"}

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

GRADER_SEVERITIES = {
    "G-CITE": "critical",
    "G-VAL": "critical",
    "G-CALC": "critical",
    "G-RULE": "critical",
    "G-SUM": "critical",
    "G-TPL": "critical",
    "G-NOTE": "major",
    "G-COMM": "critical",
    "G-PROC": "major",
}
PENDING_GRADERS = {"G-CITE", "G-NOTE", "G-COMM", "G-PROC"}

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
"""
FIXTURE_BODY = (
    "Dear {{ parties_insured_name }}, estimate {{ assessment_estimate_total }}. REF-T90"
)


def _fixture_pack(tmp_path: pathlib.Path) -> pathlib.Path:
    pack = tmp_path / "motor-fixture"
    shutil.copytree(MOTOR_PACK, pack)
    pack_yaml = pack / "pack.yaml"
    pack_yaml.write_text(
        pack_yaml.read_text().replace("version: 1.0.0", "version: 1.1.0")
    )
    (pack / "templates" / "registry.yaml").write_text(FIXTURE_REGISTRY)
    (pack / "templates" / "T-90.j2").write_text(FIXTURE_BODY)
    return pack


@pytest.fixture()
def harness(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc8.db")
    app = create_app(url)
    runtime = build_cop_runtime(app, pack_paths=[MOTOR_PACK, _fixture_pack(tmp_path)])
    evals = build_eval_harness(app)
    return TestClient(app), app, runtime, evals


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
            "money"
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


def _emit(app, event_type: str, payload: dict, claim_id: str | None = None) -> None:
    with Session(app.state.engine) as session:
        app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor="user:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            correlation_id=None,
        )
        session.commit()


def _drain(app, cycles: int = 10) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _resolve(app, capability_id: str, resolution: str = "approved", *,
             typed_kinds: list[str] | None = None, prose_ratio: float = 0.0,
             count: int = 1) -> None:
    for _ in range(count):
        _emit(
            app,
            "review.resolved",
            {
                "capability_id": capability_id,
                "resolution": resolution,
                "diff": {
                    "typed_changes": [
                        {"path": "assessment.agreed_quote", "kind": kind}
                        for kind in (typed_kinds or [])
                    ],
                    "prose_change_ratio": prose_ratio,
                },
            },
        )
    _drain(app)


def _grader_runs(app, subject_type: str | None = None) -> list[dict]:
    query = "SELECT grader_id, subject_type, result, severity FROM grader_runs"
    if subject_type:
        query += f" WHERE subject_type = '{subject_type}'"
    with app.state.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(query))]


def _events(app, event_type: str) -> list[dict]:
    with app.state.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE type = :t"), {"t": event_type}
        )
        return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def _promote(evals, capability_id: str, to_level: str, sign_offs=None):
    return evals.autonomy.request_promotion(
        capability_id, to_level, sign_offs=sign_offs or [], actor=ACTOR
    )


def _ladder_to_l2(app, evals, capability_id: str) -> None:
    _resolve(app, capability_id, count=25)
    _promote(evals, capability_id, "L2", [CM_SIGN])
    assert evals.autonomy.level(capability_id) == "L2"


def _ladder_to_l3(app, evals, capability_id: str) -> None:
    _ladder_to_l2(app, evals, capability_id)
    _resolve(app, capability_id, count=50)
    _promote(evals, capability_id, "L3", [CM_SIGN])
    assert evals.autonomy.level(capability_id) == "L3"


# --- grader registry + coverage -----------------------------------------------------


def test_grader_registry_matches_catalog(harness):
    _, _, _, evals = harness
    assert set(evals.graders.ids()) == set(GRADER_SEVERITIES)
    for grader_id, severity in GRADER_SEVERITIES.items():
        entry = evals.graders.get(grader_id)
        assert entry.severity == severity, grader_id
        expected = "pending" if grader_id in PENDING_GRADERS else "live"
        assert entry.status == expected, grader_id
        if expected == "pending":
            assert entry.blocked_on


def test_every_output_type_has_live_critical_grader(harness):
    _, _, _, evals = harness
    coverage: dict[str, list[str]] = {"field": [], "rule": [], "calc": [], "artifact": []}
    for grader_id in evals.graders.ids():
        entry = evals.graders.get(grader_id)
        if entry.status == "live" and entry.severity == "critical":
            coverage[entry.subject_type].append(grader_id)
    for subject_type, graders in coverage.items():
        assert graders, f"no live critical grader for {subject_type}"


# --- data model (§3.2 DDL binding) ---------------------------------------------------


def test_prd03_tables_exist_with_binding_columns(harness):
    _, app, _, _ = harness
    with app.state.engine.connect() as conn:
        for table, column in [
            ("test_cases", "input_bundle"),
            ("test_cases", "expected"),
            ("grader_runs", "subject_type"),
            ("grader_runs", "severity"),
            ("capabilities", "current_level"),
            ("capabilities", "max_level"),
            ("autonomy_changes", "evidence"),
            ("autonomy_changes", "approved_by"),
        ]:
            conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))


# --- production grading via the event spine ------------------------------------------


def _c02_claim(client, runtime) -> tuple[str, object]:
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
    result = runtime.execute_calc("C-02", claim_id, actor="agent:cop")
    assert result.status == "executed"
    return claim_id, result


def test_calc_output_graded_pass_on_dispatch(harness):
    client, app, runtime, _ = harness
    _c02_claim(client, runtime)
    _drain(app)
    runs = [r for r in _grader_runs(app, "calc") if r["grader_id"] == "G-CALC"]
    assert runs and all(r["result"] == "pass" for r in runs)
    assert _events(app, "grader.passed")


def test_gcalc_fails_after_input_superseded(harness):
    client, app, runtime, evals = harness
    claim_id, result = _c02_claim(client, runtime)
    _drain(app)
    _write(client, claim_id, {"assessment.agreed_quote": 250_000_00})
    graded = evals.grade(
        "G-CALC", {"calc_run_id": _latest_calc_run_id(app, claim_id)}, actor=ACTOR
    )
    assert graded.result == "fail"
    assert graded.severity == "critical"
    fails = _events(app, "grader.failed")
    assert fails
    reviews = _events(app, "review.created")
    assert any(
        p.get("type") == "EXCEPTION" and p.get("subtype") == "grader_critical_fail"
        for p in reviews
    )


def _latest_calc_run_id(app, claim_id: str) -> str:
    with app.state.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id FROM calc_runs WHERE claim_id = :c AND calc_id = 'C-02' "
                "ORDER BY ts DESC, id DESC LIMIT 1"
            ),
            {"c": claim_id},
        ).scalar()


def test_rule_run_graded_on_dispatch(harness):
    client, app, runtime, _ = harness
    claim_id = _claim(client)
    _write(
        client,
        claim_id,
        {"assessment.estimate_total": 20_000_00, "policy.excess_amount": 30_000_00},
    )
    runtime.evaluate("R-02", claim_id, actor="agent:cop")
    _drain(app)
    runs = [r for r in _grader_runs(app, "rule") if r["grader_id"] == "G-RULE"]
    assert runs and all(r["result"] == "pass" for r in runs)


def test_gsum_detects_tampered_breakdown(harness):
    client, app, runtime, evals = harness
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
    runtime.execute_calc("C-02", claim_id, actor="agent:cop")
    c03 = runtime.execute_calc("C-03", claim_id, actor="agent:cop")
    assert c03.status == "executed"
    with app.state.engine.begin() as conn:
        run_id = conn.execute(
            text(
                "SELECT id FROM calc_runs WHERE claim_id = :c AND calc_id = 'C-03'"
            ),
            {"c": claim_id},
        ).scalar()
        output = list(c03.output)
        output[0] = dict(output[0])
        output[0]["amount"] = output[0]["amount"] + 1_00
        conn.execute(
            text("UPDATE calc_runs SET output = :o WHERE id = :i"),
            {"o": json.dumps(output), "i": run_id},
        )
    graded = evals.grade("G-SUM", {"calc_run_id": run_id}, actor=ACTOR)
    assert graded.result == "fail"


def test_gval_validates_registration_field(harness):
    client, app, _, evals = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"vehicle.reg": "KDA 123B"})
    good = evals.grade(
        "G-VAL", {"claim_id": claim_id, "path": "vehicle.reg"}, actor=ACTOR
    )
    assert good.result == "pass"
    _write(client, claim_id, {"vehicle.reg": "NOT A REG"})
    bad = evals.grade(
        "G-VAL", {"claim_id": claim_id, "path": "vehicle.reg"}, actor=ACTOR
    )
    assert bad.result == "fail"


def test_gval_unmapped_path_is_error_not_silent_pass(harness):
    client, _, _, evals = harness
    claim_id = _claim(client)
    _write(client, claim_id, {"salvage.logbook_held": True})
    graded = evals.grade(
        "G-VAL", {"claim_id": claim_id, "path": "salvage.logbook_held"}, actor=ACTOR
    )
    assert graded.result == "error"


def test_gtpl_passes_render_and_fails_on_leak(harness):
    client, app, runtime, evals = harness
    claim_id = _claim(client, "motor@1.1.0")
    _write(
        client,
        claim_id,
        {"parties.insured.name": "Wanjiku Kamau", "assessment.estimate_total": 250_000_00},
    )
    rendered = runtime.render("T-90", claim_id, actor="agent:cop")
    _drain(app)
    runs = [r for r in _grader_runs(app, "artifact") if r["grader_id"] == "G-TPL"]
    assert runs and all(r["result"] == "pass" for r in runs)

    app.state.blob_store.put(rendered.blob_key, b"Dear {{ parties_insured_name }}")
    graded = evals.grade(
        "G-TPL",
        {"claim_id": claim_id, "template_id": "T-90", "blob_key": rendered.blob_key},
        actor=ACTOR,
    )
    assert graded.result == "fail"


# --- autonomy: seeding + constitution -------------------------------------------------


def test_capabilities_seeded_with_constitution_ceilings(harness):
    client, _, _, evals = harness
    assert evals.autonomy.level("intake.acknowledge") == "L1"
    assert evals.autonomy.level("project.icon.salvage_register") == "L0"
    r = client.get("/eval/capabilities", headers=AGENT)
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["capabilities"]}
    assert rows["triage.ex_gratia"]["max_level"] == "L1"
    assert rows["assessment.consistency_flag"]["max_level"] == "L2"
    assert rows["pack.note_draft"]["max_level"] == "L3"
    assert "salvage.award" not in rows


def test_policy_loader_rejects_widened_ceiling_and_forbidden_ids(harness, tmp_path):
    from eval_harness.policies import load_policies

    for body in (
        "capabilities:\n  - {id: triage.ex_gratia, max_level: L3}\n",
        "capabilities:\n  - {id: salvage.award, max_level: L1}\n",
        "capabilities:\n  - {id: approval.grant, max_level: L1}\n",
    ):
        bad = tmp_path / "policies.yaml"
        bad.write_text(body)
        with pytest.raises(ValueError):
            load_policies(bad)


# --- autonomy: counters + promotion ----------------------------------------------------


def test_consecutive_24_is_not_enough(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=24)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "intake.acknowledge", "L2", [CM_SIGN])
    assert excinfo.value.code == "CRITERIA_NOT_MET"


def test_consecutive_25_without_sign_off_is_403(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=25)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "intake.acknowledge", "L2")
    assert excinfo.value.code == "SIGN_OFF_REQUIRED"


def test_promotion_succeeds_with_evidence_event_and_ledger(harness):
    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=25)
    _promote(evals, "intake.acknowledge", "L2", [CM_SIGN])
    assert evals.autonomy.level("intake.acknowledge") == "L2"
    with app.state.engine.connect() as conn:
        change = conn.execute(
            text(
                "SELECT reason, evidence, approved_by FROM autonomy_changes "
                "WHERE capability_id = 'intake.acknowledge'"
            )
        ).fetchone()
        assert change is not None
        assert change[0] == "promotion"
        ledger = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_ledger WHERE action = 'autonomy.promoted'"
            )
        ).scalar()
    assert _events(app, "autonomy.promoted")
    assert ledger >= 1


def test_material_edit_resets_consecutive_counter(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=24)
    _resolve(app, "intake.acknowledge", "edited", typed_kinds=["money"])
    assert evals.autonomy.evidence("intake.acknowledge")["consecutive_approvals"] == 0
    _resolve(app, "intake.acknowledge", count=1)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "intake.acknowledge", "L2", [CM_SIGN])
    assert excinfo.value.code == "CRITERIA_NOT_MET"


def test_formatting_only_edit_counts_as_pass(harness):
    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=24)
    _resolve(app, "intake.acknowledge", "edited", prose_ratio=0.05)
    _promote(evals, "intake.acknowledge", "L2", [CM_SIGN])
    assert evals.autonomy.level("intake.acknowledge") == "L2"


def test_settlement_promotion_is_gp1_gated(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "settlement.eft_match", count=25)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "settlement.eft_match", "L1", [CM_SIGN])
    assert excinfo.value.code == "GATE_GP1_CLOSED"


def test_frozen_flag_blocks_all_promotions(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "intake.acknowledge", count=25)
    with app.state.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO platform_state (key, value, updated_at) "
                "VALUES ('autonomy_promotions_frozen', 'true', CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "intake.acknowledge", "L2", [CM_SIGN])
    assert excinfo.value.code == "PROMOTIONS_FROZEN"


def test_ceiling_is_never_exceeded(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _resolve(app, "triage.ex_gratia", count=25)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "triage.ex_gratia", "L2", [CM_SIGN, MD_SIGN])
    assert excinfo.value.code == "CEILING_EXCEEDED"


def test_unknown_capability_is_denied(harness):
    from eval_harness import PromotionDenied

    _, _, _, evals = harness
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "made.up_capability", "L2", [CM_SIGN])
    assert excinfo.value.code == "UNKNOWN_CAPABILITY"


def test_l4_requires_two_distinct_sign_offs(harness):
    from eval_harness import PromotionDenied

    _, app, _, evals = harness
    _ladder_to_l3(app, evals, "intake.acknowledge")
    _resolve(app, "intake.acknowledge", count=100)
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(evals, "intake.acknowledge", "L4", [CM_SIGN])
    assert excinfo.value.code == "SIGN_OFF_REQUIRED"
    with pytest.raises(PromotionDenied) as excinfo:
        _promote(
            evals,
            "intake.acknowledge",
            "L4",
            [CM_SIGN, {"actor": CM_SIGN["actor"], "role": "md"}],
        )
    assert excinfo.value.code == "SIGN_OFF_REQUIRED"
    _promote(evals, "intake.acknowledge", "L4", [CM_SIGN, MD_SIGN])
    assert evals.autonomy.level("intake.acknowledge") == "L4"


def test_l3_promotion_sets_sampling_rate(harness):
    client, app, _, evals = harness
    _ladder_to_l3(app, evals, "intake.acknowledge")
    r = client.get("/eval/capabilities", headers=AGENT)
    rows = {row["id"]: row for row in r.json()["capabilities"]}
    assert rows["intake.acknowledge"]["sampling_rate"] == 20


def test_sampling_selector_is_the_exact_formula(harness):
    _, _, _, evals = harness
    for run_id in ("run-a", "run-b", "01JZX0DEADBEEF", "another-run-id"):
        expected = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) % 100
        assert evals.autonomy.should_sample(run_id, 20) is (expected < 20)
        assert evals.autonomy.should_sample(run_id, 0) is False
        assert evals.autonomy.should_sample(run_id, 100) is True


# --- autonomy: demotion ------------------------------------------------------------------


def test_critical_failure_at_l3_demotes_within_one_cycle_and_pages(harness):
    _, app, _, evals = harness
    _ladder_to_l3(app, evals, "intake.acknowledge")
    _emit(
        app,
        "grader.failed",
        {
            "grader_id": "G-CALC",
            "severity": "critical",
            "capability_id": "intake.acknowledge",
            "subject_ref": {"calc_run_id": "synthetic"},
        },
    )
    _drain(app)
    assert evals.autonomy.level("intake.acknowledge") == "L2"
    assert _events(app, "autonomy.demoted")
    assert any(
        "intake.acknowledge" in json.dumps(p) for p in _events(app, "ops.alert")
    )
    with app.state.engine.connect() as conn:
        reason = conn.execute(
            text(
                "SELECT reason FROM autonomy_changes WHERE capability_id = "
                "'intake.acknowledge' ORDER BY occurred_at DESC LIMIT 1"
            )
        ).scalar()
    assert reason == "auto_demotion"


def test_rolling_20_below_95_percent_demotes(harness):
    _, app, _, evals = harness
    _ladder_to_l2(app, evals, "intake.acknowledge")
    _resolve(app, "intake.acknowledge", count=18)
    _resolve(app, "intake.acknowledge", "rejected", count=2)
    assert evals.autonomy.level("intake.acknowledge") == "L1"


# --- reporting API (§3.6) -----------------------------------------------------------------


def test_reporting_endpoints_are_first_class(harness):
    client, app, _, _ = harness
    r = client.get("/eval/capabilities", headers=AGENT)
    assert r.status_code == 200
    row = next(
        row for row in r.json()["capabilities"] if row["id"] == "intake.acknowledge"
    )
    assert {"id", "current_level", "max_level", "runs_to_promotion"} <= set(row)

    r = client.get("/eval/runs?capability=intake.acknowledge", headers=AGENT)
    assert r.status_code == 200

    r = client.get("/eval/corpus/stats", headers=AGENT)
    assert r.status_code == 200
    assert r.json()["total"] == 0

    r = client.get("/eval/series", headers=AGENT)
    assert r.status_code == 200
    assert {
        "autonomy_rate",
        "no_touch_rate",
        "accuracy_by_capability",
        "median_review_time_seconds",
    } <= set(r.json())


def test_promotion_http_route_returns_403_with_code(harness):
    client, app, _, _ = harness
    r = client.post(
        "/eval/capabilities/triage.ex_gratia/promote",
        json={"to_level": "L2", "sign_offs": [CM_SIGN, MD_SIGN]},
        headers=HUMAN,
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CEILING_EXCEEDED"
