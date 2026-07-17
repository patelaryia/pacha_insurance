"""PACKET-16 acceptance — PRD-07 §7.2–§7.4 assessment orchestration slice 1:
vendor registry, estimate-verified trigger, dual-path mode decision, assessor
dispatch, assessor_turnaround SLA, warn-day reminder via the chase engine.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-16_assessment_dispatch.md §3.1. No broker, browser, live
Graph mailbox, network call or live model call is permitted: `email.received`
events are synthetic, the mailbox classifier is the injected seam, the PRD-01
pipeline runs on injected fakes, and time is a FixedClock (all instants fall
inside the AR-3a send window). T-11 and T-06r-assessor bodies stay
`pending_capture` (item 6/#61/#189): every dispatch and reminder terminates
in a visible staged draft — `email.sent` never fires here (#157). R-06 stays
`blocked_on_inputs` (Q-02): the mode verdict is always `undetermined` and the
officer's choice is labelled training data (§7.3 Path A). Path B is shadow:
its output must be unreachable from every surface (PRD-07 acceptance 4).
"""
from __future__ import annotations

import base64
import io
import json
import os
import pathlib
from datetime import UTC, datetime, timedelta

import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:assessment"
OFFICER_A = "user:01HASSESSOFFICERA00000AAAA"
OFFICER_B = "user:01HASSESSOFFICERB00000AAAA"
MANAGER = "user:01HASSESSMANAGER000000AAAA"
SELF_ADDRESS = "claims@mayfair.co.ke"
BROKER_ADDR = "broker@abcbrokers.co.ke"

# Monday 2026-07-20 12:00 EAT — inside the AR-3a window; every advance below
# lands Mon–Sat at 12:00 EAT on no fixed-date Kenyan holiday.
T0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

# A rationale sentinel that must never escape agent_runs (§3.1 shadow pins).
SHADOW_RATIONALE = "SHADOW-RATIONALE-8f3d-NEVER-SURFACED"

VENDORS = [
    {"id": "V-ALPHA", "kind": "assessor", "name": "Alpha Assessors",
     "emails": ["alpha@assessors.co.ke"],
     "fee_schedule": {"physical": 638000, "desk": 0, "reinspection": 290000},
     "active": True},
    {"id": "V-BETA", "kind": "assessor", "name": "Beta Loss Adjusters",
     "emails": ["beta@adjusters.co.ke"],
     "fee_schedule": {"physical": 638000, "desk": 0, "reinspection": 290000},
     "active": True},
    {"id": "V-DORMANT", "kind": "assessor", "name": "Dormant Assessors",
     "emails": ["dormant@assessors.co.ke"],
     "fee_schedule": {"physical": 638000, "desk": 0, "reinspection": 290000},
     "active": False},
    {"id": "V-GARAGE", "kind": "garage", "name": "Kamau Motors",
     "emails": ["workshop@kamau.co.ke"],
     "fee_schedule": {}, "active": True},
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

# KES 90,000 = 9_000_000 cents — safely above the SI-600,000_00 excess of
# 15,000_00 so triage never enters the R-02 decline path.
ESTIMATE_FIELDS = {
    "fields": [
        {"name": "total", "value": "KES 90,000", "anchor_text": "KES 90,000",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
    ]
}

COVERAGE_PAYLOAD_FIELDS = {
    "policy.sum_insured": 600_000_00,
    "policy.period_start": "2026-01-01",
    "policy.period_end": "2026-12-31",
    "policy.endorsement_ref": "END-2026-001",
    "policy.premium_paid": True,
    "policy.excess_protector": False,
}


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
    def __init__(self) -> None:
        self.calls = 0

    def words(self, page_png: bytes) -> list[dict]:
        assert page_png
        self.calls += 1
        return [{"text": "assess", "bbox": [0.05, 0.05, 0.15, 0.09]}]


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


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _decode(row: dict) -> dict:
    for key in ("payload", "steps", "fee_schedule", "emails"):
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


def _open_items(app, claim_id: str, type_: str) -> list[dict]:
    return [
        item
        for item in _items(app, claim_id=claim_id, type=type_)
        if item["status"] == "open"
    ]


def _drafts(app, claim_id: str, capability_id: str) -> list[dict]:
    return [
        item
        for item in _items(app, claim_id=claim_id, type="DRAFT_RELEASE")
        if item["payload"].get("capability_id") == capability_id
    ]


def _field(client, claim_id: str, path: str) -> dict | None:
    response = client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A))
    assert response.status_code == 200, response.text
    return response.json()["fields"].get(path)


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
    # I001 suppressed: ordered for the post-build state — `assessment_agent`
    # becomes first-party only once the builder creates agents/assessment_agent
    # (#174 precedent; avoids a protected-file flip on the implementation PR).
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


def _resolve_coverage(env: Env, claim_id: str) -> None:
    card = next(
        item
        for item in _items(env.app, claim_id=claim_id, type="FIELD_VERIFY")
        if item["status"] == "open" and item["subtype"] == "coverage_manual"
    )
    response = _resolve(
        env, card["id"], OFFICER_A, action="approve",
        schema_version="FIELD_VERIFY_COVERAGE@1",
        payload={"capability_id": "triage.coverage_check", "diff": _diff(),
                 "fields": dict(COVERAGE_PAYLOAD_FIELDS)},
    )
    assert response.status_code == 200, response.text
    _advance(env)


def _model() -> TaskModel:
    """Intimation, then one estimate PDF + one damage photo, then the shadow."""

    return TaskModel({
        "document_classify": [
            {"doc_type": "intimation_email", "confidence": 0.99},
            {"doc_type": "repair_estimate", "confidence": 0.99},
            {"doc_type": "photo_damage", "confidence": 0.97},
        ],
        "extract": [INTIMATION_FIELDS, ESTIMATE_FIELDS, {"fields": []}],
        "assessment_mode_shadow": [
            {"mode": "physical", "rationale": SHADOW_RATIONALE,
             "confidence": 0.83},
        ],
    })


def _status(env: Env, claim_id: str) -> str:
    response = env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A))
    assert response.status_code == 200, response.text
    return response.json()["status"]


def _to_triaged(env: Env) -> str:
    """Drive one clean no-estimate intimation to TRIAGED (register #149)."""

    _set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    _emit_email(env, conversation_id="conv-assess-1", body=BODY_CLEAN,
                subject="New motor claim intimation")
    _advance(env)
    claim_id = _rows(
        env.app, "SELECT id FROM claims ORDER BY created_at, id"
    )[0]["id"]
    _approve_ack(env, claim_id)
    _resolve_coverage(env, claim_id)
    assert _status(env, claim_id) == "TRIAGED"
    return claim_id


def _send_estimate(env: Env) -> None:
    _emit_email(
        env, conversation_id="conv-assess-1",
        body="Repair estimate and damage photo attached",
        attachments=(
            ("estimate.pdf", "application/pdf",
             _pdf(["Repair estimate. Grand Total: KES 90,000"])),
            ("damage.png", "image/png", _png()),
        ),
    )
    _advance(env)


def _to_mode_card(env: Env) -> tuple[str, dict]:
    claim_id = _to_triaged(env)
    _send_estimate(env)
    cards = _open_items(env.app, claim_id, "MODE_CONFIRM")
    assert len(cards) == 1, cards
    return claim_id, cards[0]


def _approve_mode(env: Env, card: dict, *, mode: str,
                  vendor_ids: list[str], actor: str = OFFICER_A):
    return _resolve(
        env, card["id"], actor, action="approve",
        schema_version="MODE_CONFIRM@2",
        payload={"capability_id": "assessment.mode_confirm", "diff": _diff(),
                 "decision": {"mode": mode, "vendor_ids": vendor_ids}},
    )


def _parties(env: Env, claim_id: str, role: str) -> list[dict]:
    return _rows(
        env.app,
        "SELECT id, role, email, meta FROM parties "
        "WHERE claim_id = :c AND role = :r ORDER BY id",
        c=claim_id, r=role,
    )


# --- Pack, policy, schema and SLA pins (§1.6, §3.1) ----------------------------------


def test_pack_policy_schema_and_sla_pins():
    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in policies["capabilities"]}
    assert rows["assessment.dispatch"]["max_level"] == "L3"
    assert rows["assessment.dispatch"]["initial_level"] == "L1"
    assert rows["assessment.mode_shadow"]["max_level"] == "L0"
    assert rows["assessment.mode_shadow"]["initial_level"] == "L0"
    # Guide §6 precedence over PRD-07 §7.3: capped L2 while R-06 is blocked (#185).
    assert rows["assessment.mode_confirm"]["max_level"] == "L2"

    vendors = yaml.safe_load(
        (MOTOR_PACK / "vendors" / "vendors.yaml").read_text(encoding="utf-8"))
    assert vendors["version"] == 1
    assert vendors["standard_fees"] == {
        "physical": 638000, "desk": 0, "reinspection": 290000,
    }
    # Real firm seeds await the embed capture (#186) — never invented.
    assert vendors["vendors"] == []

    definitions = yaml.safe_load(
        (REPO / "platform" / "claim_core" / "sla" / "definitions.yaml")
        .read_text(encoding="utf-8"))
    row = next(d for d in definitions["definitions"]
               if d["id"] == "assessor_turnaround")
    assert row["status"] == "live"
    assert row["start_event"] == "assessment.dispatched"
    assert row["stop_event"] == "assessment.report_received"
    assert row["key_field"] == "assessor_party_id"
    assert row["warn_after"] == "3d"
    assert row["breach_after"] == "5d"
    assert row["calendar"] == "business"
    assert row["escalate_to_role"] == "pending_capture"

    items = yaml.safe_load(
        (MOTOR_PACK / "checklists" / "items.yaml").read_text(encoding="utf-8"))
    assert items["items"]["assessor_report"] == {
        "kind": "document", "doc_type": "assessor_report", "physical": False,
    }

    registry = yaml.safe_load(
        (MOTOR_PACK / "templates" / "registry.yaml").read_text(encoding="utf-8"))
    templates = {row["id"]: row for row in registry["templates"]}
    assert templates["T-11"]["status"] == "pending_capture"
    assert templates["T-06r-assessor"]["status"] == "pending_capture"

    schema = json.loads(
        (MOTOR_PACK / "review" / "schemas" / "MODE_CONFIRM@2.json")
        .read_text(encoding="utf-8"))
    assert "decision" in schema["required"]
    decision = schema["properties"]["decision"]
    assert decision["properties"]["mode"]["enum"] == ["desk", "physical"]
    assert decision["properties"]["vendor_ids"]["minItems"] == 1
    # @1 stays registered for replay (#183).
    assert (MOTOR_PACK / "review" / "schemas" / "MODE_CONFIRM@1.json").exists()
    contracts = yaml.safe_load(
        (MOTOR_PACK / "review" / "contracts.yaml").read_text(encoding="utf-8"))
    assert contracts["types"]["MODE_CONFIRM"]["resolution_schema"] == "MODE_CONFIRM@2"
    # The review-type enum stays closed at 17 (PRD-04 §4.3).
    assert len(contracts["types"]) == 17


# --- Vendor registry (§7.2) ----------------------------------------------------------


def test_vendor_read_route_filters_kind_and_active(tmp_path):
    env = _build(tmp_path, "vendors", model=_model())
    response = env.client.get("/vendors", params={"kind": "assessor"},
                              headers=_h(OFFICER_A))
    assert response.status_code == 200, response.text
    vendors = response.json()["vendors"]
    assert [vendor["id"] for vendor in vendors] == ["V-ALPHA", "V-BETA"]
    assert all(vendor["active"] for vendor in vendors)
    assert vendors[0]["fee_schedule"]["physical"] == 638000
    assert vendors[0]["fee_schedule"]["desk"] == 0
    assert vendors[0]["fee_schedule"]["reinspection"] == 290000

    # Actor identity is mandatory (PACKET-01 D-3 posture).
    assert env.client.get("/vendors").status_code in (401, 422)


# --- Estimate trigger, FSM advance, Path A card (§7.3) -------------------------------


def test_estimate_commit_advances_fsm_and_opens_undetermined_mode_card(tmp_path):
    env = _build(tmp_path, "trigger", model=_model())
    claim_id, card = _to_mode_card(env)

    # Stepwise hops, event per hop (#182): TRIAGED → AWAITING_DOCS → IN_ASSESSMENT.
    assert _status(env, claim_id) == "IN_ASSESSMENT"
    hops = [(event["payload"]["from"], event["payload"]["to"])
            for event in _events(env.app, "claim.status_changed", claim_id)]
    assert ("TRIAGED", "AWAITING_DOCS") in hops
    assert ("AWAITING_DOCS", "IN_ASSESSMENT") in hops

    # The card shows estimate, threshold verdict, photos strip (§7.3 verbatim).
    action_payload = card["payload"]["action"]["payload"]
    assert action_payload["estimate_total"] == 9_000_000
    assert action_payload["rule"]["rule_id"] == "R-06"
    assert action_payload["rule"]["status"] == "blocked_on_inputs"
    assert action_payload["rule"]["verdict"] == "undetermined"
    assert action_payload["rule"]["rule_run_id"]
    assert len(action_payload["photos"]) == 1
    assert action_payload["estimate_document_id"]
    assert _events(env.app, "assessment.mode_item_created", claim_id)

    # The estimate itself came through the chase checklist (PACKET-15 seam).
    estimate = _field(env.client, claim_id, "assessment.estimate_total")
    assert estimate is not None and estimate["value"] == 9_000_000

    # Redelivery/duplicate versions while the card is open are no-ops (#182).
    _advance(env)
    assert len(_open_items(env.app, claim_id, "MODE_CONFIRM")) == 1


def test_second_estimate_while_card_open_is_noop(tmp_path):
    model = _model()
    model.responses["document_classify"].append(
        {"doc_type": "repair_estimate", "confidence": 0.99})
    model.responses["extract"].append({
        "fields": [
            {"name": "total", "value": "KES 95,000", "anchor_text": "KES 95,000",
             "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
        ]
    })
    env = _build(tmp_path, "dupe-estimate", model=model)
    claim_id, _card = _to_mode_card(env)

    _emit_email(env, conversation_id="conv-assess-1", body="Revised estimate",
                attachments=(("estimate2.pdf", "application/pdf",
                              _pdf(["Revised estimate. Grand Total: KES 95,000"])),))
    _advance(env)
    assert len(_open_items(env.app, claim_id, "MODE_CONFIRM")) == 1
    # Exactly one shadow run per issued card (§3.1).
    shadow_calls = [call for call in env.model.calls
                    if call["inputs"]["task"] == "assessment_mode_shadow"]
    assert len(shadow_calls) == 1


# --- Path B shadow (§7.3, PRD-07 acceptance 4) ---------------------------------------


def test_shadow_runs_at_l0_and_never_surfaces(tmp_path):
    env = _build(tmp_path, "shadow", model=_model())
    claim_id, card = _to_mode_card(env)

    shadow_calls = [call for call in env.model.calls
                    if call["inputs"]["task"] == "assessment_mode_shadow"]
    assert len(shadow_calls) == 1
    assert shadow_calls[0]["tier"] == "MODEL_HEAVY"
    # Vehicle age has no registered path: recorded null, never derived (#192).
    assert shadow_calls[0]["inputs"]["vehicle_age"] is None

    runs = _rows(
        env.app,
        "SELECT id, capability_id, level FROM agent_runs "
        "WHERE capability_id = 'assessment.mode_shadow'",
    )
    assert len(runs) == 1
    assert runs[0]["level"] == "L0"

    # Never surfaced: no review item, event, or claim read carries the output.
    for item in _items(env.app, claim_id=claim_id):
        assert SHADOW_RATIONALE not in json.dumps(item["payload"])
    event_rows = _rows(env.app, "SELECT payload FROM events")
    assert all(SHADOW_RATIONALE not in json.dumps(row["payload"])
               for row in event_rows)
    claim_read = env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A))
    assert SHADOW_RATIONALE not in claim_read.text
    assert "mode_shadow" not in json.dumps(card["payload"])


# --- Mode resolution → multi-assessor dispatch (§7.2/§7.4) ---------------------------


def test_physical_multi_assessor_dispatch_stages_t11_per_firm(tmp_path):
    env = _build(tmp_path, "dispatch", model=_model())
    claim_id, card = _to_mode_card(env)

    response = _approve_mode(env, card, mode="physical",
                             vendor_ids=["V-ALPHA", "V-BETA"])
    assert response.status_code == 200, response.text
    _drain(env.app)

    # Officer decision committed as human-source fields (#184).
    mode = _field(env.client, claim_id, "assessment.mode")
    assert mode is not None and mode["value"] == "physical"
    assert mode["source_type"] == "human"
    multi = _field(env.client, claim_id, "assessment.multi_mode")
    assert multi is not None and multi["value"] is True
    decided = _events(env.app, "assessment.mode_decided", claim_id)
    assert len(decided) == 1
    assert decided[0]["payload"]["mode"] == "physical"
    assert set(decided[0]["payload"]["vendor_ids"]) == {"V-ALPHA", "V-BETA"}

    # Vendor→party bridge (#187): one claim-scoped assessor party per firm.
    assessors = _parties(env, claim_id, "assessor")
    assert len(assessors) == 2
    assessor_ids = {party["id"] for party in assessors}
    brokers = _parties(env, claim_id, "broker")
    assert len(brokers) == 1

    # One staged T-11 per firm at assessment.dispatch L1; cc broker as
    # recipient (#168 mirror); pending-template posture (#157/#189).
    drafts = _drafts(env.app, claim_id, "assessment.dispatch")
    assert len(drafts) == 2
    vendor_ids = set()
    for draft in drafts:
        action = draft["payload"]["action"]
        payload = action["payload"]
        assert payload["template_id"] == "T-11"
        assert payload["mode"] == "physical"
        vendor_ids.add(payload["vendor_id"])
        merge = payload["merge"]
        assert {"claim_ref", "insured_name", "vehicle_reg", "loss_summary",
                "mode"} <= set(merge)
        # Garage details have no committed source yet — visible gap, never
        # invented (§3.1).
        assert payload["missing"]
        held = set(payload["attachments"])
        assert len(held) == 2  # estimate + photo; the rest are missing
        assert set(payload["missing_attachments"]) == {"claim_form", "logbook"}
        recipients = set(action["to_party_ids"])
        assert brokers[0]["id"] in recipients
        assert recipients & assessor_ids
    assert vendor_ids == {"V-ALPHA", "V-BETA"}

    # Dispatch events started per-firm SLA clocks (#191); nothing was sent.
    dispatched = _events(env.app, "assessment.dispatched", claim_id)
    assert len(dispatched) == 2
    assert {event["payload"]["vendor_id"] for event in dispatched} == vendor_ids
    clocks = _rows(
        env.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :c "
        "AND definition_id = 'assessor_turnaround' AND stopped_at IS NULL",
        c=claim_id,
    )
    assert len(clocks) == 2
    assert _events(env.app, "email.sent") == []

    # One assessor_report checklist per firm, requester = that assessor (#190).
    checklists = _rows(
        env.app,
        "SELECT id, purpose, requester_party_id FROM chase_checklists "
        "WHERE claim_id = :c AND purpose = 'assessor_report' ORDER BY id",
        c=claim_id,
    )
    assert len(checklists) == 2
    assert {row["requester_party_id"] for row in checklists} == assessor_ids
    chase_items = _rows(
        env.app,
        "SELECT ci.item_id, ci.state FROM chase_items ci "
        "JOIN chase_checklists cc ON cc.id = ci.checklist_id "
        "WHERE cc.claim_id = :c AND cc.purpose = 'assessor_report'",
        c=claim_id,
    )
    assert len(chase_items) == 2
    assert all(row["item_id"] == "assessor_report" for row in chase_items)
    assert all(row["state"] == "requested" for row in chase_items)

    # Re-approval cannot double-dispatch (§5 idempotency).
    replay = _approve_mode(env, card, mode="physical",
                           vendor_ids=["V-ALPHA", "V-BETA"])
    assert replay.status_code in (200, 409)
    assert len(_drafts(env.app, claim_id, "assessment.dispatch")) == 2


def test_desk_single_vendor_sets_no_multi_flag(tmp_path):
    env = _build(tmp_path, "desk", model=_model())
    claim_id, card = _to_mode_card(env)

    response = _approve_mode(env, card, mode="desk", vendor_ids=["V-ALPHA"])
    assert response.status_code == 200, response.text
    _drain(env.app)

    mode = _field(env.client, claim_id, "assessment.mode")
    assert mode is not None and mode["value"] == "desk"
    assert _field(env.client, claim_id, "assessment.multi_mode") is None
    assert len(_drafts(env.app, claim_id, "assessment.dispatch")) == 1


# --- Negative paths (§3.1) -----------------------------------------------------------


def test_unknown_or_inactive_vendor_422_and_reject_reissues(tmp_path):
    env = _build(tmp_path, "negative", model=_model())
    claim_id, card = _to_mode_card(env)

    for bad in (["V-NOPE"], ["V-DORMANT"], ["V-ALPHA", "V-NOPE"]):
        response = _approve_mode(env, card, mode="physical", vendor_ids=bad)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "VENDOR_NOT_REGISTERED"
    assert _drafts(env.app, claim_id, "assessment.dispatch") == []
    assert len(_open_items(env.app, claim_id, "MODE_CONFIRM")) == 1

    # Reject re-issues a fresh card linked by retry_of (mirror #152).
    response = _resolve(
        env, card["id"], OFFICER_A, action="reject",
        schema_version="MODE_CONFIRM@2",
        payload={"capability_id": "assessment.mode_confirm", "diff": _diff(),
                 "reason": "photos unclear",
                 "decision": {"mode": "physical", "vendor_ids": ["V-ALPHA"]}},
    )
    assert response.status_code == 200, response.text
    _drain(env.app)
    reissued = _open_items(env.app, claim_id, "MODE_CONFIRM")
    assert len(reissued) == 1
    assert reissued[0]["id"] != card["id"]
    assert reissued[0]["payload"]["retry_of"] == card["id"]
    assert _status(env, claim_id) == "IN_ASSESSMENT"


# --- Warn-day assessor reminder via the chase engine (§7.4) --------------------------


def test_assessor_reminder_stages_t06r_assessor_at_warn(tmp_path):
    env = _build(tmp_path, "reminder", model=_model())
    claim_id, card = _to_mode_card(env)
    assert _approve_mode(env, card, mode="physical",
                         vendor_ids=["V-ALPHA"]).status_code == 200
    _drain(env.app)
    assessor_id = _parties(env, claim_id, "assessor")[0]["id"]

    _at(env, days=3, hours=1)
    result = env.app.state.chase_agent.tick()
    assert result["sent"] >= 1
    _drain(env.app)

    reminders = [
        draft for draft in _drafts(env.app, claim_id, "chase.reminder")
        if draft["payload"]["action"]["payload"].get("template_id")
        == "T-06r-assessor"
    ]
    assert len(reminders) == 1
    action = reminders[0]["payload"]["action"]
    assert action["to_party_ids"] == [assessor_id]
    outstanding = action["payload"]["outstanding"]
    assert [row["item_id"] for row in outstanding] == ["assessor_report"]
    assert _events(env.app, "email.sent") == []
    assert _events(env.app, "chase.reminder_sent") == []

    # The per-firm turnaround clock is still running — the stop event belongs
    # to PACKET-17 (§0 seam).
    clocks = _rows(
        env.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :c "
        "AND definition_id = 'assessor_turnaround' AND stopped_at IS NULL",
        c=claim_id,
    )
    assert len(clocks) == 1
    assert _events(env.app, "assessment.report_received") == []
