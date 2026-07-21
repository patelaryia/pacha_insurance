"""PACKET-17 acceptance — PRD-07 §7.5–§7.7 assessment cascade: report
attribution, write-off gate, multi-assessor selection, reserves, savings
ledger, consistency flags. PRD-07 complete.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-17_assessment_cascade.md §3.1. No broker, browser, live
Graph mailbox, network call or live model call is permitted: `email.received`
events are synthetic, the PRD-01 pipeline runs on injected fakes, and time is
a FixedClock. Because T-11 was never really sent (item 1/#130/#157), assessor
replies attach via the PRD-05 reference-match path (vehicle reg in the
subject/body — #201) and attribute by sender address against the #187
assessor parties. Fees are never auto-committed (#203 mirror #167): C-02
blocks visibly until the officer keys them. FX-1 (§7.6) is the canonical
savings fixture; reporting sums header rows only.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from datetime import UTC, datetime, timedelta

import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

OFFICER_A = "user:01HCASCADEOFFICERA0000AAAA"
OFFICER_B = "user:01HCASCADEOFFICERB0000AAAA"
MANAGER = "user:01HCASCADEMANAGER00000AAAA"
SELF_ADDRESS = "claims@mayfair.co.ke"
BROKER_ADDR = "broker@abcbrokers.co.ke"
ALPHA_ADDR = "alpha@assessors.co.ke"
BETA_ADDR = "beta@adjusters.co.ke"
STRANGER_ADDR = "stranger@example.co.ke"

# Monday 2026-07-20 12:00 EAT — inside the AR-3a window.
T0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

VENDORS = [
    {"id": "V-ALPHA", "kind": "assessor", "name": "Alpha Assessors",
     "emails": [ALPHA_ADDR],
     "fee_schedule": {"physical": 638000, "desk": 0, "reinspection": 290000},
     "active": True},
    {"id": "V-BETA", "kind": "assessor", "name": "Beta Loss Adjusters",
     "emails": [BETA_ADDR],
     "fee_schedule": {"physical": 638000, "desk": 0, "reinspection": 290000},
     "active": True},
]

BODY_CLEAN = (
    "Jane Wanjiku reports vehicle KBX 123A collided at Mombasa Road "
    "junction on 2026-07-01"
)

INTIMATION_FIELDS = {
    "fields": [
        {"name": "insured_name", "value": "Jane Wanjiku",
         "anchor_text": "Jane Wanjiku", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
        {"name": "reg", "value": "KBX 123A", "anchor_text": "KBX 123A",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
        {"name": "loss_date", "value": "2026-07-01",
         "anchor_text": "2026-07-01", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
        {"name": "narrative",
         "value": "vehicle KBX 123A collided at Mombasa Road junction",
         "anchor_text": "vehicle KBX 123A collided at Mombasa Road junction",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
    ]
}


def _money_field(name: str, kes: str) -> dict:
    return {"name": name, "value": f"KES {kes}", "anchor_text": f"KES {kes}",
            "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"}


# FX-1 (§7.6 canonical): estimate KES 261,000; agreed KES 136,276; garage
# door line KES 139,200 vs supplier Kawama KES 48,000.
FX1_ESTIMATE_KES = "261,000"
FX1_ESTIMATE_CENTS = 26_100_000
FX1_AGREED_CENTS = 13_627_600
FX1_HEADER_SAVING = 12_472_400
FX1_LINE_SAVING = 9_120_000

FX1_REPORT_FIELDS = {
    "fields": [
        _money_field("agreed_quote", "136,276"),
        _money_field("pav", "1,500,000"),
        _money_field("assessor_fee", "6,380"),
        {"name": "assessor_firm", "value": "Alpha Assessors",
         "anchor_text": "Alpha Assessors", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
        # Shilling-denominated ints, as the PRD-01 schema example shows;
        # the cascade applies money_kes normalisation (×100) before the
        # ledger write (#206) — never raw units into a BIGINT cents column.
        {"name": "supplier_lines",
         "value": [{"part": "garage door panel", "supplier": "Kawama",
                    "supplier_price": 48_000, "garage_price": 139_200}],
         "anchor_text": "Kawama 48,000", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
        {"name": "flags", "value": ["repairable"],
         "anchor_text": "repairable", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
    ]
}

FX1_REPORT_PDF_LINES = [
    "Assessor report for KBX 123A. Alpha Assessors",
    "Agreed quote KES 136,276. PAV KES 1,500,000. Fee KES 6,380",
    "Supplier line: garage door panel Kawama 48,000 vs garage 139,200",
    "Recommendation repairable",
]


class FixedClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, value: datetime) -> None:
        self.now = value


class FakeClassifier:
    def __init__(self) -> None:
        self.script: list[dict] = []
        self.calls: list[dict] = []

    def classify(self, message: dict) -> dict:
        self.calls.append(message)
        if not self.script:
            raise AssertionError("classifier called without a scripted result")
        return self.script.pop(0)


class FakeOcr:
    def words(self, page_png: bytes) -> list[dict]:
        assert page_png
        return [{"text": "cascade", "bbox": [0.05, 0.05, 0.15, 0.09]}]


class TaskModel:
    """Strict fake: every model call must declare a scripted stable task."""

    def __init__(self, responses: dict[str, list[dict] | dict]) -> None:
        self.responses = {
            task: list(value) if isinstance(value, list) else [value]
            for task, value in responses.items()
        }
        self.calls: list[dict] = []

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        task = inputs["task"]
        self.calls.append({"tier": tier, "schema": schema, "inputs": dict(inputs)})
        queue = self.responses.get(task, [])
        if not queue:
            raise AssertionError(f"unexpected model task {task!r}")
        data = queue.pop(0)
        return {"data": data, "cost_usd": 0.01, "model_id": f"fake-{tier.lower()}"}


def _h(actor: str) -> dict[str, str]:
    return {"X-Actor": actor}


def _pdf(pages: list[str]) -> bytes:
    import fitz

    document = fitz.open()
    for value in pages:
        page = document.new_page()
        page.insert_text((72, 72), value)
    return document.tobytes()


def _report_pdf(lines: list[str]) -> bytes:
    """A single-page report: every field cites page 1, so all anchor text
    must live on one page (owner fixture correction, #212)."""
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "\n".join(lines))
    return document.tobytes()


def _decode(row: dict) -> dict:
    for key in ("payload", "evidence", "steps"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key])
            except ValueError:
                pass
    return row


def _rows(app, sql: str, **params) -> list[dict]:
    with app.state.engine.connect() as connection:
        return [
            _decode(dict(row))
            for row in connection.execute(text(sql), params).mappings()
        ]


def _events(app, event_type: str, claim_id: str | None = None) -> list[dict]:
    rows = _rows(
        app,
        "SELECT id, claim_id, payload FROM events WHERE type = :t ORDER BY seq",
        t=event_type,
    )
    if claim_id is None:
        return rows
    return [row for row in rows if row["claim_id"] == claim_id]


def _items(app, **filters) -> list[dict]:
    clauses = " AND ".join(f"{key} = :{key}" for key in filters) or "1=1"
    return _rows(
        app,
        "SELECT id, claim_id, type, subtype, status, payload FROM review_items "
        f"WHERE {clauses} ORDER BY created_at, id",
        **filters,
    )


def _open_items(app, claim_id: str, type_: str, subtype: str | None = None) -> list[dict]:
    return [
        item
        for item in _items(app, claim_id=claim_id, type=type_)
        if item["status"] == "open" and (subtype is None or item["subtype"] == subtype)
    ]


def _field(client, claim_id: str, path: str) -> dict | None:
    response = client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A))
    assert response.status_code == 200, response.text
    return response.json()["fields"].get(path)


def _status(env, claim_id: str) -> str:
    response = env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A))
    assert response.status_code == 200, response.text
    return response.json()["status"]


def _set_level(app, capability_id: str, level: str) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
            {"level": level, "id": capability_id},
        )


def _drain(app, cycles: int = 96) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


class Env:
    def __init__(self, app, client, classifier, clock, model) -> None:
        self.app = app
        self.client = client
        self.classifier = classifier
        self.clock = clock
        self.model = model
        self.processed: set[str] = set()
        self.message_seq = 0


def _build(tmp_path, name: str, *, model: TaskModel) -> Env:
    # I001 suppressed: ordered for the post-build state (mirror PACKET-16).
    from fastapi.testclient import TestClient  # noqa: I001

    from agent_runtime import build_agent_runtime
    from assessment_agent import build_assessment_agent
    from chase_agent import build_chase_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from doc_intel.engine import build_engine
    from eval_harness import build_eval_harness
    from intake_agent import build_intake_agent
    from review_queue import build_review_queue

    clock = FixedClock()
    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(url, clock=clock)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(
        app,
        roles={
            OFFICER_A: "claims_officer",
            OFFICER_B: "claims_officer",
            MANAGER: "claims_manager",
        },
    )
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=clock)
    build_agent_runtime(app)
    classifier = FakeClassifier()
    build_intake_agent(
        app,
        classifier=classifier,
        officers=[OFFICER_A, OFFICER_B],
        config={"self_addresses": [SELF_ADDRESS], "archive_sample_rate": 0},
    )
    build_chase_agent(app)
    build_assessment_agent(app, model_client=model, config={"vendors": VENDORS})
    return Env(app, TestClient(app), classifier, clock, model)


def _emit_email(
    env: Env,
    *,
    conversation_id: str,
    body: str,
    subject: str = "Motor claim correspondence",
    attachments: tuple[tuple[str, str, bytes], ...] = (),
    from_addr: str = BROKER_ADDR,
) -> str:
    from sqlalchemy.orm import Session

    env.message_seq += 1
    payload = {
        "graph_message_id": f"msg-{env.message_seq:03d}",
        "conversation_id": conversation_id,
        "from_addr": from_addr,
        "to_addrs": [SELF_ADDRESS],
        "subject": subject,
        "body_text": body,
        "attachments": [
            {"filename": filename, "mime": mime,
             "content_b64": base64.b64encode(content).decode("ascii")}
            for filename, mime, content in attachments
        ],
    }
    with Session(env.app.state.engine) as session:
        event = env.app.state.record_event(
            session,
            claim_id=None,
            event_type="email.received",
            payload=payload,
            actor="system",
            correlation_id=None,
        )
        session.commit()
        return event.id


def _advance(env: Env, cycles: int = 8) -> None:
    for _ in range(cycles):
        _drain(env.app)
        pending = [
            row["id"]
            for row in _rows(env.app, "SELECT id FROM documents ORDER BY id")
            if row["id"] not in env.processed
        ]
        if not pending:
            break
        for document_id in pending:
            env.processed.add(document_id)
            env.app.state.doc_intel.process_document(document_id)
    _drain(env.app)


def _at(env: Env, days: int, hours: int = 0) -> None:
    env.clock.advance_to(T0 + timedelta(days=days, hours=hours))


def _resolve(env: Env, item_id: str, actor: str, *, action: str,
             schema_version: str, payload: dict):
    return env.client.post(
        f"/reviews/{item_id}/resolve",
        json={"action": action, "schema_version": schema_version,
              "payload": payload},
        headers=_h(actor),
    )


def _diff() -> dict:
    return {"typed_changes": [], "prose_change_ratio": 0}


def _write(env: Env, claim_id: str, values: dict) -> None:
    writes = []
    for path, value in values.items():
        value_type = (
            "money" if isinstance(value, int) and not isinstance(value, bool)
            else "bool" if isinstance(value, bool)
            else "string"
        )
        writes.append({
            "path": path, "value": value, "value_type": value_type,
            "source_type": "human", "verification_state": "human_verified",
        })
    r = env.client.patch(
        f"/claims/{claim_id}/fields", json={"writes": writes},
        headers=_h(OFFICER_A),
    )
    assert r.status_code == 200, r.text


def _approve_ack(env: Env, claim_id: str) -> None:
    ack = next(
        item
        for item in _items(env.app, claim_id=claim_id, type="DRAFT_RELEASE")
        if item["status"] == "open"
        and item["payload"].get("action", {}).get("payload", {}).get("template_id")
        == "T-06a"
    )
    response = _resolve(
        env, ack["id"], OFFICER_A, action="approve",
        schema_version="DRAFT_RELEASE@1",
        payload={"capability_id": "intake.acknowledge", "diff": _diff()},
    )
    assert response.status_code == 200, response.text
    _advance(env)


def _resolve_coverage(env: Env, claim_id: str, *, sum_insured: int = 600_000_00) -> None:
    card = next(
        item
        for item in _items(env.app, claim_id=claim_id, type="FIELD_VERIFY")
        if item["status"] == "open" and item["subtype"] == "coverage_manual"
    )
    response = _resolve(
        env, card["id"], OFFICER_A, action="approve",
        schema_version="FIELD_VERIFY_COVERAGE@1",
        payload={
            "capability_id": "triage.coverage_check", "diff": _diff(),
            "fields": {
                "policy.sum_insured": sum_insured,
                "policy.period_start": "2026-01-01",
                "policy.period_end": "2026-12-31",
                "policy.endorsement_ref": "END-2026-001",
                "policy.premium_paid": True,
                "policy.excess_protector": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    _advance(env)


def _base_model(*, estimate_kes: str, report_fields: dict | None = None,
                extra_classify: tuple = (), extra_extract: tuple = ()) -> TaskModel:
    classify = [
        {"doc_type": "intimation_email", "confidence": 0.99},
        {"doc_type": "repair_estimate", "confidence": 0.99},
    ]
    extract = [
        INTIMATION_FIELDS,
        {"fields": [_money_field("total", estimate_kes)]},
    ]
    if report_fields is not None:
        classify.append({"doc_type": "assessor_report", "confidence": 0.99})
        extract.append(report_fields)
    classify.extend(extra_classify)
    extract.extend(extra_extract)
    return TaskModel({
        "document_classify": classify,
        "extract": extract,
        "assessment_mode_shadow": [
            {"mode": "physical", "rationale": "shadow", "confidence": 0.8},
        ],
    })


def _to_dispatched(env: Env, *, vendor_ids: list[str], estimate_kes: str,
                   mode: str = "physical",
                   sum_insured: int = 600_000_00) -> str:
    """Intimation → triage → estimate reply → mode approve → staged dispatch."""

    _set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    _emit_email(env, conversation_id="conv-cascade-1", body=BODY_CLEAN,
                subject="New motor claim intimation")
    _advance(env)
    claim_id = _rows(
        env.app, "SELECT id FROM claims ORDER BY created_at, id"
    )[0]["id"]
    _approve_ack(env, claim_id)
    _resolve_coverage(env, claim_id, sum_insured=sum_insured)
    _emit_email(
        env, conversation_id="conv-cascade-1", body="Repair estimate attached",
        attachments=(("estimate.pdf", "application/pdf",
                      _pdf([f"Repair estimate. Grand Total: KES {estimate_kes}"])),),
    )
    _advance(env)
    card = _open_items(env.app, claim_id, "MODE_CONFIRM")[0]
    response = _resolve(
        env, card["id"], OFFICER_A, action="approve",
        schema_version="MODE_CONFIRM@2",
        payload={"capability_id": "assessment.mode_confirm", "diff": _diff(),
                 "decision": {"mode": mode, "vendor_ids": vendor_ids}},
    )
    assert response.status_code == 200, response.text
    _drain(env.app)
    assert len(_events(env.app, "assessment.dispatched", claim_id)) == len(vendor_ids)
    return claim_id


def _assessor_party_ids(env: Env, claim_id: str) -> dict[str, str]:
    rows = _rows(
        env.app,
        "SELECT id, meta FROM parties WHERE claim_id = :c AND role = 'assessor' "
        "ORDER BY id",
        c=claim_id,
    )
    result = {}
    for row in rows:
        meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"])
        result[meta["vendor_id"]] = row["id"]
    return result


def _send_report(env: Env, *, from_addr: str = ALPHA_ADDR,
                 pdf_lines: list[str] | None = None) -> None:
    _emit_email(
        env, conversation_id=f"conv-assessor-{env.message_seq}",
        subject="Assessor report for KBX 123A",
        body="Report attached for vehicle KBX 123A",
        from_addr=from_addr,
        attachments=(("report.pdf", "application/pdf",
                      _report_pdf(pdf_lines or FX1_REPORT_PDF_LINES)),),
    )
    _advance(env)


def _open_clocks(env: Env, claim_id: str) -> list[dict]:
    return _rows(
        env.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :c "
        "AND definition_id = 'assessor_turnaround' AND stopped_at IS NULL",
        c=claim_id,
    )


def _savings(env: Env, claim_id: str) -> list[dict]:
    return _rows(
        env.app,
        "SELECT kind, baseline_amount, achieved_amount, saving, vendor_id, "
        "evidence FROM savings_ledger WHERE claim_id = :c ORDER BY kind, id",
        c=claim_id,
    )


# --- Pack pins (§3.1) ----------------------------------------------------------------


def test_pack_pins_reserve_field_and_r07_still_blocked():
    fields = yaml.safe_load(
        (MOTOR_PACK / "fields.yaml").read_text(encoding="utf-8"))
    assert fields["fields"]["reserve.total"] == {
        "value_type": "money", "pii_class": "none",
    }
    r07 = yaml.safe_load(
        (MOTOR_PACK / "rules" / "R-07.yaml").read_text(encoding="utf-8"))
    assert r07["status"] == "blocked_on_inputs"
    dashboard = yaml.safe_load(
        (MOTOR_PACK / "dashboard.yaml").read_text(encoding="utf-8"))
    row = next(s for s in dashboard["series"] if s["id"] == "savings_mtd_ytd")
    assert row["status"] == "live"


# --- Scenario 1 + 5: single assessor, FX-1, fees keyed, MTD header-only --------------


def test_scenario_1_fx1_report_cascade_reserve_savings_flags(tmp_path):
    model = _base_model(estimate_kes=FX1_ESTIMATE_KES,
                        report_fields=FX1_REPORT_FIELDS)
    env = _build(tmp_path, "fx1", model=model)
    claim_id = _to_dispatched(env, vendor_ids=["V-ALPHA"],
                              estimate_kes=FX1_ESTIMATE_KES)
    parties = _assessor_party_ids(env, claim_id)
    assert len(_open_clocks(env, claim_id)) == 1

    _send_report(env)

    # Attribution + SLA stop + chase completion (§7.5 / #191 / PACKET-15 reuse).
    received = _events(env.app, "assessment.report_received", claim_id)
    assert len(received) == 1
    assert received[0]["payload"]["assessor_party_id"] == parties["V-ALPHA"]
    assert received[0]["payload"]["vendor_id"] == "V-ALPHA"
    assert received[0]["payload"]["document_id"]
    assert _open_clocks(env, claim_id) == []
    assert any(
        event["payload"].get("item_id") == "assessor_report"
        for event in _events(env.app, "chase.item_verified", claim_id)
    )

    # C1: report parsed → REPORT_RECEIVED; R-05 false here (quote×2 < min).
    assert _status(env, claim_id) == "REPORT_RECEIVED"
    assert _field(env.client, claim_id, "assessment.write_off_indicated") is None

    # Committed financials from the report (extraction target paths).
    agreed = _field(env.client, claim_id, "assessment.agreed_quote")
    assert agreed is not None and agreed["value"] == FX1_AGREED_CENTS

    # C3 blocked visibly on the un-committed fees (#203 — never substituted).
    blocked = _rows(
        env.app,
        "SELECT status, missing_inputs FROM calc_runs "
        "WHERE claim_id = :c AND calc_id = 'C-02' ORDER BY ts, id",
        c=claim_id,
    )
    assert blocked and blocked[0]["status"] == "blocked_on_inputs"
    assert "assessment.assessor_fee" in str(blocked[0]["missing_inputs"])
    assert _field(env.client, claim_id, "reserve.total") is None

    # Officer keys the fees from the report → cascade re-attempts (#203).
    _write(env, claim_id, {
        "assessment.assessor_fee": 638_000,
        "assessment.reinspection_fee": 0,
    })
    _drain(env.app)

    reserve = _field(env.client, claim_id, "reserve.total")
    assert reserve is not None
    assert reserve["value"] == FX1_AGREED_CENTS + 638_000
    assert reserve["source_type"] == "calc"
    projection = _events(env.app, "projection.requested", claim_id)
    assert len(projection) == 1
    assert projection[0]["payload"]["reserve_total"] == FX1_AGREED_CENTS + 638_000
    assert projection[0]["payload"]["calc_run_id"]

    # C-03 stays visibly blocked (#49/#58 posture unchanged).
    c03 = _rows(
        env.app,
        "SELECT status FROM calc_runs WHERE claim_id = :c AND calc_id = 'C-03' "
        "ORDER BY ts DESC, id DESC",
        c=claim_id,
    )
    assert c03 == [] or c03[0]["status"] == "blocked_on_inputs"

    # C4: FX-1 exact — header (billable) + line (evidence), citations mandatory.
    rows = _savings(env, claim_id)
    header = [r for r in rows if r["kind"] == "assessment_negotiation"]
    lines = [r for r in rows if r["kind"] == "supplier_substitution"]
    assert len(header) == 1 and len(lines) == 1
    assert header[0]["baseline_amount"] == FX1_ESTIMATE_CENTS
    assert header[0]["achieved_amount"] == FX1_AGREED_CENTS
    assert header[0]["saving"] == FX1_HEADER_SAVING
    assert header[0]["evidence"]["calc_run_id"]
    assert header[0]["evidence"]["citations"]
    assert lines[0]["baseline_amount"] == 13_920_000
    assert lines[0]["achieved_amount"] == 4_800_000
    assert lines[0]["saving"] == FX1_LINE_SAVING
    assert lines[0]["vendor_id"] is None
    assert "Kawama" in json.dumps(lines[0]["evidence"])
    assert _events(env.app, "savings.recorded", claim_id)

    # Scenario 5: MTD tile sums header rows only (§7.6 verbatim). The line
    # row exists but must not contaminate the billable figure.
    portfolio = env.client.get("/portfolio", headers=_h(MANAGER))
    assert portfolio.status_code == 200, portfolio.text
    tile = next(row for row in portfolio.json()["tiles"]
                if row["series_id"] == "savings_mtd_ytd")
    assert tile["status"] == "live"
    assert tile["data"]["mtd"] == FX1_HEADER_SAVING
    assert tile["data"]["ytd"] == FX1_HEADER_SAVING

    # C5: assessor flags surface as CONSISTENCY_FLAG at the L2-capped path.
    flags = _open_items(env.app, claim_id, "CONSISTENCY_FLAG")
    assert any("repairable" in json.dumps(item["payload"]) for item in flags)

    # Redelivery is a no-op: still one header row, one report_received.
    _advance(env)
    assert len([r for r in _savings(env, claim_id)
                if r["kind"] == "assessment_negotiation"]) == 1
    assert len(_events(env.app, "assessment.report_received", claim_id)) == 1


# --- Scenario 2: R-05 strictly-greater boundary --------------------------------------


def _boundary_report(quote_kes: str, pav_kes: str) -> dict:
    return {"fields": [
        _money_field("agreed_quote", quote_kes),
        _money_field("pav", pav_kes),
    ]}


def test_scenario_2_write_off_boundary(tmp_path):
    # Case A: quote×2 == min(pav, si) exactly → NOT a write-off (R-05 strict >).
    model_a = _base_model(estimate_kes="200,000",
                          report_fields=_boundary_report("130,000", "260,000"))
    env_a = _build(tmp_path, "boundary-eq", model=model_a)
    claim_a = _to_dispatched(env_a, vendor_ids=["V-ALPHA"], estimate_kes="200,000")
    _send_report(env_a, pdf_lines=[
        "Assessor report for KBX 123A",
        "Agreed quote KES 130,000. PAV KES 260,000",
    ])
    assert _status(env_a, claim_a) == "REPORT_RECEIVED"
    assert _field(env_a.client, claim_a, "assessment.write_off_indicated") is None

    # Case B: one shilling over the boundary → WRITE_OFF transition (§7.7-2).
    model_b = _base_model(estimate_kes="200,000",
                          report_fields=_boundary_report("130,001", "260,000"))
    env_b = _build(tmp_path, "boundary-over", model=model_b)
    claim_b = _to_dispatched(env_b, vendor_ids=["V-ALPHA"], estimate_kes="200,000")
    _send_report(env_b, pdf_lines=[
        "Assessor report for KBX 123A",
        "Agreed quote KES 130,001. PAV KES 260,000",
    ])
    assert _status(env_b, claim_b) == "WRITE_OFF"
    indicated = _field(env_b.client, claim_b, "assessment.write_off_indicated")
    assert indicated is not None and indicated["value"] is True
    hops = [(e["payload"]["from"], e["payload"]["to"])
            for e in _events(env_b.app, "claim.status_changed", claim_b)]
    assert ("IN_ASSESSMENT", "REPORT_RECEIVED") in hops
    assert ("REPORT_RECEIVED", "WRITE_OFF") in hops


# --- Scenario 3: multi-assessor, one non-responder, proceed-with-received ------------


def test_scenario_3_multi_assessor_breach_proceed_partial(tmp_path):
    model = _base_model(estimate_kes=FX1_ESTIMATE_KES,
                        report_fields=FX1_REPORT_FIELDS)
    env = _build(tmp_path, "multi", model=model)
    claim_id = _to_dispatched(env, vendor_ids=["V-ALPHA", "V-BETA"],
                              estimate_kes=FX1_ESTIMATE_KES)
    parties = _assessor_party_ids(env, claim_id)
    assert len(_open_clocks(env, claim_id)) == 2

    _send_report(env, from_addr=ALPHA_ADDR)

    # Alpha's clock stopped, Beta's still running; selection pending (§7.5 C2).
    assert len(_open_clocks(env, claim_id)) == 1
    assert _events(env.app, "assessment.selection_completed", claim_id) == []
    assert _open_items(env.app, claim_id, "PROCEED_PARTIAL") == []

    # Breach the outstanding clock (business calendar 5d) → PROCEED_PARTIAL.
    _at(env, days=10)
    env.app.state.sla_engine.evaluate()
    _drain(env.app)
    partials = _open_items(env.app, claim_id, "PROCEED_PARTIAL")
    assert len(partials) == 1
    payload = partials[0]["payload"]
    assert payload["received"] == [parties["V-ALPHA"]]
    assert payload["outstanding"] == [parties["V-BETA"]]

    # Re-evaluate: still exactly one open item (idempotent).
    env.app.state.sla_engine.evaluate()
    _drain(env.app)
    assert len(_open_items(env.app, claim_id, "PROCEED_PARTIAL")) == 1

    # Officer approves proceed-with-received → R-07 selection over one firm.
    response = _resolve(
        env, partials[0]["id"], OFFICER_A, action="approve",
        schema_version="PROCEED_PARTIAL@1",
        payload={"capability_id": "assessment.selection", "diff": _diff()},
    )
    assert response.status_code == 200, response.text
    _drain(env.app)

    selected = _events(env.app, "assessment.selection_completed", claim_id)
    assert len(selected) == 1
    body = selected[0]["payload"]
    assert body["selected_party_id"] == parties["V-ALPHA"]
    assert body["rule_id"] == "R-07"
    assert len(body["comparison"]) == 1
    assert body["comparison"][0]["assessor_party_id"] == parties["V-ALPHA"]
    assert body["comparison"][0]["agreed_quote"] == FX1_AGREED_CENTS

    # Cascade proceeded with the received firm's committed values.
    assert _status(env, claim_id) == "REPORT_RECEIVED"
    agreed = _field(env.client, claim_id, "assessment.agreed_quote")
    assert agreed is not None and agreed["value"] == FX1_AGREED_CENTS

    # Officer override after selection → EXCEPTION{selection_overridden} (§7.5).
    _write(env, claim_id, {"assessment.agreed_quote": 14_000_000})
    _drain(env.app)
    overrides = _open_items(env.app, claim_id, "EXCEPTION", "selection_overridden")
    assert len(overrides) == 1
    # The human value stands — never reverted (append-only, guide §3.5).
    final = _field(env.client, claim_id, "assessment.agreed_quote")
    assert final is not None and final["value"] == 14_000_000


# --- Negative: unattributable report never cascades ----------------------------------


def test_unattributed_report_raises_exception_and_no_cascade(tmp_path):
    model = _base_model(estimate_kes=FX1_ESTIMATE_KES,
                        report_fields=FX1_REPORT_FIELDS)
    env = _build(tmp_path, "unattributed", model=model)
    claim_id = _to_dispatched(env, vendor_ids=["V-ALPHA"],
                              estimate_kes=FX1_ESTIMATE_KES)

    _send_report(env, from_addr=STRANGER_ADDR)

    items = _open_items(env.app, claim_id, "EXCEPTION", "report_unattributed")
    assert len(items) == 1
    payload = items[0]["payload"]
    assert {"facts", "risk", "recommendation", "resolution_schema"} <= set(payload)
    assert _events(env.app, "assessment.report_received", claim_id) == []
    assert len(_open_clocks(env, claim_id)) == 1
    assert _status(env, claim_id) == "IN_ASSESSMENT"
    assert _savings(env, claim_id) == []
