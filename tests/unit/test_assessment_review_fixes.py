"""Regression probes for the PACKET-17 blocking review findings."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


def _acceptance_module(filename: str, module_name: str):
    path = Path(__file__).resolve().parents[1] / f"acceptance/{filename}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P16 = _acceptance_module(
    "test_packet_16_assessment_dispatch.py", "packet16_review_fix_helpers"
)
P17 = _acceptance_module(
    "test_packet_17_assessment_cascade.py", "packet17_review_fix_helpers"
)


def test_shadow_budget_breach_is_ledgered_and_awaits_exception_review(tmp_path):
    env = P16._build(tmp_path, "shadow-budget", model=P16._model())
    env.app.state.assessment_agent.trigger.shadow_config["max_cost_usd"] = 0.001

    claim_id, _card = P16._to_mode_card(env)
    P16._drain(env.app)

    runs = P16._rows(
        env.app,
        "SELECT status, steps FROM agent_runs "
        "WHERE capability_id = 'assessment.mode_shadow'",
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "awaiting_review"
    assert runs[0]["steps"][-1]["outcome"]["log_payload"]["error_type"] == (
        "budget_exceeded"
    )
    exceptions = [
        item
        for item in P16._items(env.app, claim_id=claim_id, type="EXCEPTION")
        if item["subtype"] == "budget_exceeded"
    ]
    assert len(exceptions) == 1
    calls = P16._events(env.app, "model.called", claim_id)
    shadow = [
        event
        for event in calls
        if event["payload"].get("detail", {}).get("task")
        == "assessment_mode_shadow"
    ]
    assert len(shadow) == 1
    assert shadow[0]["payload"]["detail"]["prompt_ref"] == (
        "assessment_mode_shadow@1"
    )


def test_shadow_model_call_starts_after_durable_l0_run(tmp_path):
    env = P16._build(tmp_path, "shadow-run-first", model=P16._model())
    original_call = env.model.structured_call

    def checked_call(*, tier, schema, inputs):
        if inputs.get("task") == "assessment_mode_shadow":
            rows = P16._rows(
                env.app,
                "SELECT status, autonomy_level FROM agent_runs "
                "WHERE capability_id = 'assessment.mode_shadow'",
            )
            assert rows == [{"status": "running", "autonomy_level": "L0"}]
        return original_call(tier=tier, schema=schema, inputs=inputs)

    env.model.structured_call = checked_call

    P16._to_mode_card(env)


def test_supplier_rows_require_resolved_cite_stage_evidence(tmp_path):
    report = copy.deepcopy(P17.FX1_REPORT_FIELDS)
    supplier = next(
        field for field in report["fields"] if field["name"] == "supplier_lines"
    )
    supplier["anchor_text"] = "anchor absent from the report"
    model = P17._base_model(
        estimate_kes=P17.FX1_ESTIMATE_KES,
        report_fields=report,
    )
    env = P17._build(tmp_path, "supplier-citation", model=model)
    claim_id = P17._to_dispatched(
        env,
        vendor_ids=["V-ALPHA"],
        estimate_kes=P17.FX1_ESTIMATE_KES,
    )

    P17._send_report(
        env,
        pdf_lines=[
            "Assessor report for KBX 123A. Alpha Assessors",
            "Agreed quote KES 136,276. PAV KES 1,500,000. Fee KES 6,380",
            "Recommendation repairable",
        ],
    )

    rows = P17._savings(env, claim_id)
    header = [row for row in rows if row["kind"] == "assessment_negotiation"]
    lines = [row for row in rows if row["kind"] == "supplier_substitution"]
    assert len(header) == 1
    assert lines == []
    assert header[0]["evidence"]["incomplete_lines"] == [
        {
            "index": 0,
            "part": "garage door panel",
            "supplier": "Kawama",
            "reason": "unresolved_citation",
        }
    ]


def test_same_firm_report_revisions_fail_closed_before_selection(tmp_path):
    model = P17._base_model(
        estimate_kes=P17.FX1_ESTIMATE_KES,
        report_fields=P17.FX1_REPORT_FIELDS,
        extra_classify=(
            {"doc_type": "assessor_report", "confidence": 0.99},
        ),
        extra_extract=(P17.FX1_REPORT_FIELDS,),
    )
    env = P17._build(tmp_path, "report-revisions", model=model)
    claim_id = P17._to_dispatched(
        env,
        vendor_ids=["V-ALPHA", "V-BETA"],
        estimate_kes=P17.FX1_ESTIMATE_KES,
    )

    P17._send_report(env, from_addr=P17.ALPHA_ADDR)
    P17._send_report(env, from_addr=P17.ALPHA_ADDR)

    assert P17._events(env.app, "assessment.selection_completed", claim_id) == []
    exceptions = [
        item
        for item in P17._open_items(env.app, claim_id, "EXCEPTION")
        if item["subtype"] == "assessment_report_revision_ambiguous"
    ]
    assert len(exceptions) == 1
    assert len(exceptions[0]["payload"]["facts"]["document_ids"]) == 2


def test_corrected_fee_appends_reserve_and_projection_for_new_c02_inputs(tmp_path):
    model = P17._base_model(
        estimate_kes=P17.FX1_ESTIMATE_KES,
        report_fields=P17.FX1_REPORT_FIELDS,
    )
    env = P17._build(tmp_path, "corrected-fee", model=model)
    claim_id = P17._to_dispatched(
        env,
        vendor_ids=["V-ALPHA"],
        estimate_kes=P17.FX1_ESTIMATE_KES,
    )
    P17._send_report(env)
    P17._write(
        env,
        claim_id,
        {
            "assessment.assessor_fee": 638_000,
            "assessment.reinspection_fee": 0,
        },
    )
    P17._drain(env.app)

    P17._write(env, claim_id, {"assessment.assessor_fee": 700_000})
    P17._drain(env.app)

    projections = P17._events(env.app, "projection.requested", claim_id)
    assert len(projections) == 2
    assert [row["payload"]["reserve_total"] for row in projections] == [
        P17.FX1_AGREED_CENTS + 638_000,
        P17.FX1_AGREED_CENTS + 700_000,
    ]
    reserves = P17._rows(
        env.app,
        "SELECT value, version, superseded_by FROM claim_fields "
        "WHERE claim_id = :claim_id AND path = 'reserve.total' ORDER BY version",
        claim_id=claim_id,
    )
    assert len(reserves) == 2
    assert reserves[0]["superseded_by"] is not None
    assert reserves[1]["superseded_by"] is None
