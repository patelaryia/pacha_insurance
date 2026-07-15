"""PACKET-14 acceptance — PRD-05 §5.3 S1–S8 intake flow as a durable AR-1
COP run, §5.4 Mode A triage + decline path, dupe/late checks, KPI wire.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-14_intake_flow.md §3.1. No broker, browser, live Graph
mailbox, network call or live model call is permitted: `email.received`
events are synthetic (register #120), the mailbox classifier is the injected
seam, and the PRD-01 pipeline runs on the injected TaskModel/FakeOcr fakes.
Scenario wall-clock numbers (<5 min, <15 min inbox) are live-trial gates
(proposed #146); this suite proves the mechanics.
"""
from __future__ import annotations

import base64
import io
import json
import os
import pathlib

import pytest
import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER_A = "user:01HINTAKEOFFICERA00000AAAA"
OFFICER_B = "user:01HINTAKEOFFICERB00000AAAA"
MANAGER = "user:01HINTAKEMANAGER000000AAAA"
SELF_ADDRESS = "claims@mayfair.co.ke"
BROKER_ADDR = "broker@abcbrokers.co.ke"

INTAKE_STEP_IDS = [
    "create_claim", "ingest", "populate", "dupe_check",
    "late_check", "acknowledge", "checklist", "triage",
]

BASE_CHECKLIST_IDS = [
    "claim_form", "logbook_copy", "dl_copy", "kra_pin",
    "police_abstract", "repair_estimate", "photos",
]

BODY_CLEAN = (
    "Jane Wanjiku reports vehicle KBX 123A collided at Mombasa Road "
    "junction on 2026-07-01"
)
# §5.2 ref-match scans subject/body literally; the hyphenated plate evades the
# literal scan (observed typography variant) so classification and the S4
# dupe check — not the router reference match — must catch the duplicate.
BODY_DUPE = (
    "Our client reports that vehicle KBX-123A was involved in a collision "
    "on 2026-07-04 along Thika Road"
)

INTIMATION_FIELDS_CLEAN = {
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

INTIMATION_FIELDS_DUPE = {
    "fields": [
        {"name": "reg", "value": "KBX 123A", "anchor_text": "KBX-123A",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
        {"name": "loss_date", "value": "2026-07-04",
         "anchor_text": "2026-07-04", "page": 1, "confidence": 1.0,
         "citation_mode": "anchor_text"},
    ]
}

ESTIMATE_FIELDS = {
    "fields": [
        {"name": "total", "value": "KES 15,000", "anchor_text": "KES 15,000",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
    ]
}

COVERAGE_PATHS = [
    "policy.sum_insured", "policy.period_start", "policy.period_end",
    "policy.endorsement_ref", "policy.premium_paid", "policy.excess_protector",
]


class FakeClassifier:
    """Injected §5.2 classifier seam; returns a queued script of results."""

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
        return [
            {"text": "intake", "bbox": [0.05, 0.05, 0.15, 0.09]},
            {"text": "page", "bbox": [0.16, 0.05, 0.27, 0.09]},
        ]


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


def _decode(row: dict) -> dict:
    for key in ("payload", "steps"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
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


def _runs(app) -> list[dict]:
    return _rows(
        app,
        "SELECT id, agent, capability_id, claim_id, trigger_event, status, "
        "steps, autonomy_level FROM agent_runs ORDER BY started_at, id",
    )


def _claims(app) -> list[dict]:
    return _rows(app, "SELECT id, status, lob FROM claims ORDER BY created_at, id")


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


def _drain(app, cycles: int = 64) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


class Env:
    def __init__(self, app, client, classifier) -> None:
        self.app = app
        self.client = client
        self.classifier = classifier
        self.processed: set[str] = set()


def _build(tmp_path, name: str, *, model: TaskModel) -> Env:
    from fastapi.testclient import TestClient

    from agent_runtime import build_agent_runtime
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from doc_intel.engine import build_engine
    from eval_harness import build_eval_harness
    from intake_agent import build_intake_agent
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(url)
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
    build_engine(app, model_client=model, ocr_engine=FakeOcr())
    build_agent_runtime(app)
    classifier = FakeClassifier()
    build_intake_agent(
        app,
        classifier=classifier,
        officers=[OFFICER_A, OFFICER_B],
        config={"self_addresses": [SELF_ADDRESS], "archive_sample_rate": 0},
    )
    return Env(app, TestClient(app), classifier)


def _emit_email(
    env: Env,
    message_id: str,
    *,
    body: str,
    subject: str = "New motor claim intimation",
    attachments: tuple[tuple[str, str, bytes], ...] = (),
    from_addr: str = BROKER_ADDR,
) -> str:
    from sqlalchemy.orm import Session

    payload = {
        "graph_message_id": message_id,
        "conversation_id": f"conv-{message_id}",
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
    """Drain the bus and feed every new document through the PRD-01 fakes."""

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


def _open_item(env: Env, claim_id: str, *, type_: str,
               subtype: str | None = None,
               template_id: str | None = None) -> dict:
    candidates = [
        item
        for item in _items(env.app, claim_id=claim_id, type=type_)
        if item["status"] == "open"
        and (subtype is None or item["subtype"] == subtype)
        and (
            template_id is None
            or item["payload"].get("action", {}).get("payload", {}).get("template_id")
            == template_id
        )
    ]
    assert candidates, f"no open {type_}/{subtype or template_id} item on {claim_id}"
    return candidates[0]


def _approve_ack(env: Env, claim_id: str) -> None:
    item = _open_item(env, claim_id, type_="DRAFT_RELEASE", template_id="T-06a")
    response = _resolve(
        env, item["id"], OFFICER_A, action="approve",
        schema_version="DRAFT_RELEASE@1",
        payload={"capability_id": "intake.acknowledge", "diff": _diff()},
    )
    assert response.status_code == 200, response.text
    _advance(env)


def _coverage_payload(
    *,
    sum_insured: int = 600_000_00,
    period: tuple[str, str] = ("2026-01-01", "2026-12-31"),
    premium_paid: bool = True,
) -> dict:
    return {
        "capability_id": "triage.coverage_check",
        "diff": _diff(),
        "fields": {
            "policy.sum_insured": sum_insured,
            "policy.period_start": period[0],
            "policy.period_end": period[1],
            "policy.endorsement_ref": "END-2026-001",
            "policy.premium_paid": premium_paid,
            "policy.excess_protector": False,
        },
    }


def _resolve_coverage(env: Env, claim_id: str, **overrides) -> None:
    item = _open_item(env, claim_id, type_="FIELD_VERIFY", subtype="coverage_manual")
    response = _resolve(
        env, item["id"], OFFICER_A, action="approve",
        schema_version="FIELD_VERIFY_COVERAGE@1",
        payload=_coverage_payload(**overrides),
    )
    assert response.status_code == 200, response.text
    _advance(env)


def _start_clean_intimation(env: Env, message_id: str = "msg-intake-1",
                            **email_kwargs) -> None:
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    _emit_email(env, message_id, body=email_kwargs.pop("body", BODY_CLEAN),
                **email_kwargs)
    _advance(env)


def _clean_model(*, extra_classify: tuple = (), extra_extract: tuple = ()) -> TaskModel:
    return TaskModel({
        "document_classify": [
            {"doc_type": "intimation_email", "confidence": 0.99},
            *extra_classify,
        ],
        "extract": [INTIMATION_FIELDS_CLEAN, *extra_extract],
    })


# --- AR-1 durable run --------------------------------------------------------------


def test_intake_requested_starts_durable_run_with_verbatim_steps(tmp_path):
    env = _build(tmp_path, "run-record", model=_clean_model())
    _set_level(env.app, "intake.claim_creation", "L3")
    _start_clean_intimation(env)

    requested = _events(env.app, "intake.requested")
    assert len(requested) == 1
    runs = _runs(env.app)
    assert len(runs) == 1
    run = runs[0]
    assert run["agent"] == "intake"
    assert run["capability_id"] == "intake.claim_creation"
    assert run["trigger_event"] == requested[0]["id"]
    assert [step["step_id"] for step in run["steps"]] == INTAKE_STEP_IDS
    assert run["claim_id"], "S1 must record the created claim on the run"

    # Redelivery of the same intimation event is a no-op: still one run, one claim.
    _advance(env)
    assert len(_runs(env.app)) == 1
    assert len(_claims(env.app)) == 1


# --- Scenario 1 --------------------------------------------------------------------


def test_scenario_1_intimation_to_ack_draft_checklist_and_citations(tmp_path):
    env = _build(tmp_path, "scenario-1", model=_clean_model())
    _set_level(env.app, "intake.claim_creation", "L3")
    _start_clean_intimation(env)

    claims = _claims(env.app)
    assert len(claims) == 1
    claim_id = claims[0]["id"]
    assert claims[0]["status"] == "INTIMATED"
    assert claims[0]["lob"] == "motor"

    # Acknowledge SLA clock started on claim.created (PRD-00 §0.5, 24x7).
    clocks = _rows(
        env.app,
        "SELECT definition_id, state, stopped_at FROM sla_clocks "
        "WHERE claim_id = :c AND definition_id = 'acknowledge'",
        c=claim_id,
    )
    assert len(clocks) == 1
    assert clocks[0]["stopped_at"] is None

    # S3: fields populated with citations; provenance is mandatory (PRD-01 §1.4).
    reg = _field(env.client, claim_id, "vehicle.reg")
    assert reg is not None and reg["value"] == "KBX 123A"
    assert reg["source_ref"] and reg["source_ref"].get("citation_mode")
    loss_date = _field(env.client, claim_id, "loss.date")
    assert loss_date is not None and str(loss_date["value"]).startswith("2026-07-01")
    received = _field(env.client, claim_id, "intimation.received_at")
    assert received is not None
    assert received["source_type"] == "system"  # §5.3 via register #144
    assert received["confidence"] is None

    # S3: parties from sender + extracted insured (§5.3).
    parties = _rows(
        env.app,
        "SELECT role, name, email FROM parties WHERE claim_id = :c ORDER BY id",
        c=claim_id,
    )
    assert any(row["email"] == BROKER_ADDR for row in parties)
    assert any(row["role"] == "insured" for row in parties)

    # S5: R-10 is blocked_on_inputs (#50) — recorded, never a run pause (#138).
    late_runs = _rows(
        env.app,
        "SELECT status FROM rule_runs WHERE claim_id = :c AND rule_id = 'R-10'",
        c=claim_id,
    )
    assert late_runs and late_runs[0]["status"] == "blocked_on_inputs"

    # S6: acknowledgement drafted at launch L1 — visible pending-template body,
    # never invented prose, never a fake send.
    ack = _open_item(env, claim_id, type_="DRAFT_RELEASE", template_id="T-06a")
    action_payload = ack["payload"]["action"]["payload"]
    assert action_payload["template_status"] == "pending_capture"
    assert action_payload["body"] == "pending_capture"
    assert action_payload["signable"] is False
    assert _events(env.app, "email.sent") == []

    # Run paused awaiting the ack draft; officer release resumes it (AR-1).
    assert _runs(env.app)[0]["status"] == "awaiting_review"
    _approve_ack(env, claim_id)

    # S7: checklist live — one durable chase.init hand-off (proposed #147).
    handoffs = _events(env.app, "chase.init", claim_id)
    assert len(handoffs) == 1
    items = handoffs[0]["payload"]["items"]
    assert [item["id"] for item in items] == BASE_CHECKLIST_IDS
    assert all(item["already_received"] is False for item in items)

    # S8: Mode A coverage card open; run awaiting review (§5.4).
    card = _open_item(env, claim_id, type_="FIELD_VERIFY", subtype="coverage_manual")
    assert set(COVERAGE_PATHS) <= set(card["payload"].get("keyed_paths", []))
    assert _runs(env.app)[0]["status"] == "awaiting_review"


def test_photos_only_missing_narrative_adds_incident_description_item(tmp_path):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(image, format="PNG")
    model = TaskModel({
        "document_classify": [
            {"doc_type": "intimation_email", "confidence": 0.99},
            {"doc_type": "photo_damage", "confidence": 0.97},
        ],
        "extract": [
            {"fields": [
                {"name": "reg", "value": "KCA 456B", "anchor_text": "KCA 456B",
                 "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
            ]},
            {"fields": []},
        ],
    })
    env = _build(tmp_path, "photos-only", model=model)
    _set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.9})
    _emit_email(
        env, "msg-photos-1",
        body="Accident involving KCA 456B, photos attached",
        attachments=(("damage.png", "image/png", image.getvalue()),),
    )
    _advance(env)

    claims = _claims(env.app)
    assert len(claims) == 1, "photos-only intimation still creates the claim (§5.3)"
    claim_id = claims[0]["id"]
    _approve_ack(env, claim_id)

    handoffs = _events(env.app, "chase.init", claim_id)
    assert len(handoffs) == 1
    ids = [item["id"] for item in handoffs[0]["payload"]["items"]]
    assert ids == [*BASE_CHECKLIST_IDS, "incident_description"]


# --- Launch-level governance (S1) ---------------------------------------------------


def test_launch_l2_claim_creation_fails_closed_to_staged_confirm(tmp_path):
    env = _build(tmp_path, "launch-l2", model=_clean_model())
    # §5.6 launch level is the registered initial level: L2. gate.yaml maps no
    # confirm type for intake.claim_creation → DRAFT_RELEASE path (#126/#135).
    _start_clean_intimation(env)

    assert _claims(env.app) == [], "no claim may exist before the confirm resolves"
    runs = _runs(env.app)
    assert len(runs) == 1 and runs[0]["status"] == "awaiting_review"
    staged = [
        item for item in _items(env.app, type="DRAFT_RELEASE")
        if item["status"] == "open"
        and item["payload"].get("capability_id") == "intake.claim_creation"
    ]
    assert len(staged) == 1

    response = _resolve(
        env, staged[0]["id"], OFFICER_A, action="approve",
        schema_version="DRAFT_RELEASE@1",
        payload={"capability_id": "intake.claim_creation", "diff": _diff()},
    )
    assert response.status_code == 200, response.text
    _advance(env)
    assert len(_claims(env.app)) == 1, "officer confirm executes the creation"


# --- Scenario 4 + fraud arm ---------------------------------------------------------


def test_scenario_4_duplicate_reg_and_date_pauses_with_exception(tmp_path):
    model = _clean_model(
        extra_classify=({"doc_type": "intimation_email", "confidence": 0.99},),
        extra_extract=(INTIMATION_FIELDS_DUPE,),
    )
    env = _build(tmp_path, "scenario-4", model=model)
    _set_level(env.app, "intake.claim_creation", "L3")
    _start_clean_intimation(env, "msg-dupe-a")
    claim_a = _claims(env.app)[0]["id"]

    # Second intimation, same reg, loss date exactly +3 days (boundary inclusive).
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.9})
    _emit_email(env, "msg-dupe-b", body=BODY_DUPE)
    _advance(env)

    claims = _claims(env.app)
    assert len(claims) == 2
    claim_b = next(row["id"] for row in claims if row["id"] != claim_a)
    dupes = _items(env.app, claim_id=claim_b, type="EXCEPTION",
                   subtype="possible_duplicate")
    assert len(dupes) == 1
    assert dupes[0]["payload"]["candidates"] == [claim_a]
    run_b = next(run for run in _runs(env.app) if run["claim_id"] == claim_b)
    assert run_b["status"] == "awaiting_review", "scenario 4: the run pauses"
    assert not any(
        item["payload"].get("action", {}).get("payload", {}).get("template_id")
        == "T-06a"
        for item in _items(env.app, claim_id=claim_b, type="DRAFT_RELEASE")
    ), "no acknowledgement may be drafted while the duplicate is unresolved"

    # Officer resolution resumes the run (AR-1); it proceeds to the ack draft.
    response = _resolve(
        env, dupes[0]["id"], OFFICER_A, action="approve",
        schema_version="EXCEPTION@1",
        payload={"capability_id": "intake.claim_creation", "diff": _diff()},
    )
    assert response.status_code == 200, response.text
    _advance(env)
    assert _open_item(env, claim_b, type_="DRAFT_RELEASE", template_id="T-06a")


def test_closed_claim_duplicate_raises_fraud_signal_and_never_pauses(tmp_path):
    model = _clean_model(
        extra_classify=({"doc_type": "intimation_email", "confidence": 0.99},),
        extra_extract=(INTIMATION_FIELDS_DUPE,),
    )
    env = _build(tmp_path, "fraud-signal", model=model)
    _set_level(env.app, "intake.claim_creation", "L3")
    _start_clean_intimation(env, "msg-fraud-a")
    claim_a = _claims(env.app)[0]["id"]
    response = env.client.post(
        f"/claims/{claim_a}/transition",
        json={"to": "WITHDRAWN", "payload": {}},
        headers=_h(OFFICER_A),
    )
    assert response.status_code == 200, response.text
    _drain(env.app)

    env.classifier.script.append({"class": "new_intimation", "confidence": 0.9})
    _emit_email(env, "msg-fraud-b", body=BODY_DUPE)
    _advance(env)

    claims = _claims(env.app)
    assert len(claims) == 2
    claim_b = next(row["id"] for row in claims if row["id"] != claim_a)

    # §5.3 S4(b): NOT a pause — a fraud signal, visible and durable (#136).
    signals = _events(env.app, "fraud.signal", claim_b)
    assert len(signals) == 1
    assert signals[0]["payload"]["matched_claim_id"] == claim_a
    assert signals[0]["payload"]["matched_terminal_state"] == "WITHDRAWN"
    assert _items(env.app, claim_id=claim_b, type="EXCEPTION",
                  subtype="possible_duplicate") == []
    ack = _open_item(env, claim_b, type_="DRAFT_RELEASE", template_id="T-06a")
    assert ack, "the run continued through S6 without pausing"

    # Surfaced in triage (§5.3): the coverage card carries the signal.
    _approve_ack(env, claim_b)
    card = _open_item(env, claim_b, type_="FIELD_VERIFY", subtype="coverage_manual")
    fraud_payloads = card["payload"].get("fraud_signals", [])
    assert any(entry["matched_claim_id"] == claim_a for entry in fraud_payloads)


# --- Scenario 3: Mode A triage and the decline path ---------------------------------


def _drive_to_coverage_card(env: Env) -> str:
    _set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    _emit_email(
        env, "msg-triage-1", body=BODY_CLEAN,
        attachments=(
            ("estimate.pdf", "application/pdf",
             _pdf(["Repair estimate. Grand Total: KES 15,000"])),
        ),
    )
    _advance(env)
    claim_id = _claims(env.app)[0]["id"]
    _approve_ack(env, claim_id)
    return claim_id


def _triage_model() -> TaskModel:
    return TaskModel({
        "document_classify": [
            {"doc_type": "intimation_email", "confidence": 0.99},
            {"doc_type": "repair_estimate", "confidence": 0.99},
        ],
        "extract": [INTIMATION_FIELDS_CLEAN, ESTIMATE_FIELDS],
    })


def test_scenario_3_below_excess_boundary_decline_draft_and_ex_gratia(tmp_path):
    env = _build(tmp_path, "scenario-3", model=_triage_model())
    claim_id = _drive_to_coverage_card(env)

    # Estimate already held → the checklist marks it received (§5.5 re-send rule).
    handoff = _events(env.app, "chase.init", claim_id)[0]
    received = {item["id"]: item["already_received"]
                for item in handoff["payload"]["items"]}
    assert received["repair_estimate"] is True

    # Mode A: SI 600,000_00 → C-01 excess = 15,000_00 exactly; the extracted
    # estimate is 15,000_00 — estimate == excess fires R-02 (boundary verbatim).
    _resolve_coverage(env, claim_id)

    excess = _field(env.client, claim_id, "policy.excess_amount")
    assert excess is not None and excess["value"] == 15_000_00
    assert excess["source_type"] == "calc"
    keyed = _field(env.client, claim_id, "policy.sum_insured")
    assert keyed is not None and keyed["source_type"] == "human"

    decline = _open_item(env, claim_id, type_="DRAFT_RELEASE", subtype="decline_draft")
    assert decline["payload"]["draft_template"] == "T-07"
    ex_gratia = [item for item in _items(env.app, claim_id=claim_id, type="EX_GRATIA")
                 if item["status"] == "open"]
    assert len(ex_gratia) == 1

    # Coverage + excess evaluated → TRIAGED (PRD-00 §0.4); nothing was sent (§5.7-3).
    assert env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A)).json()[
        "status"] == "TRIAGED"
    assert _events(env.app, "email.sent") == []

    # Release blocks visibly while T-07 is pending_capture (item 6; #141).
    response = _resolve(
        env, decline["id"], OFFICER_A, action="approve",
        schema_version="DRAFT_RELEASE@1",
        payload={"capability_id": "triage.decline_draft", "diff": _diff()},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    assert env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A)).json()[
        "status"] == "TRIAGED"
    still_open = _open_item(env, claim_id, type_="DRAFT_RELEASE",
                            subtype="decline_draft")
    assert still_open["id"] == decline["id"]


def test_out_of_cover_is_an_exception_never_a_silent_pick(tmp_path):
    env = _build(tmp_path, "out-of-cover", model=_triage_model())
    claim_id = _drive_to_coverage_card(env)
    _resolve_coverage(env, claim_id, period=("2025-01-01", "2025-12-31"))

    out_of_cover = _items(env.app, claim_id=claim_id, type="EXCEPTION",
                          subtype="out_of_cover")
    assert len(out_of_cover) == 1
    assert env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A)).json()[
        "status"] == "INTIMATED", "the officer, not the agent, decides out-of-cover"


def test_premium_unpaid_is_an_officer_decision_exception(tmp_path):
    env = _build(tmp_path, "premium-unpaid", model=_triage_model())
    claim_id = _drive_to_coverage_card(env)
    _resolve_coverage(env, claim_id, premium_paid=False)

    unpaid = _items(env.app, claim_id=claim_id, type="EXCEPTION",
                    subtype="premium_unpaid")
    assert len(unpaid) == 1
    assert env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A)).json()[
        "status"] == "INTIMATED"


# --- Scenario 6 ---------------------------------------------------------------------


def test_scenario_6_l3_ack_runs_zero_touch_to_the_visible_blocked_send(tmp_path):
    env = _build(tmp_path, "scenario-6", model=_clean_model())
    _set_level(env.app, "intake.claim_creation", "L3")
    _set_level(env.app, "intake.acknowledge", "L3")
    _start_clean_intimation(env, "msg-l3-ack")

    claims = _claims(env.app)
    assert len(claims) == 1
    claim_id = claims[0]["id"]

    # Zero human touches: nothing was resolved by anyone up to the ack attempt.
    resolved = _rows(
        env.app,
        "SELECT id FROM review_items WHERE claim_id = :c AND status != 'open'",
        c=claim_id,
    )
    assert resolved == []

    # The L3 execute path terminates at the visible blocked send: no email.sent,
    # no invented body, no DRAFT_RELEASE for the ack (#120/#130/#146).
    assert _events(env.app, "email.sent") == []
    assert not any(
        item["payload"].get("action", {}).get("payload", {}).get("template_id")
        == "T-06a"
        for item in _items(env.app, claim_id=claim_id, type="DRAFT_RELEASE")
    )
    run = _runs(env.app)[0]
    ack_step = next(step for step in run["steps"] if step["step_id"] == "acknowledge")
    assert ack_step["outcome"]["status"] == "refused"

    # The refusal is not a run failure: the flow continued to S8 (Mode A card).
    assert _open_item(env, claim_id, type_="FIELD_VERIFY", subtype="coverage_manual")
    assert run["status"] == "awaiting_review"


# --- KPI wire + pack/data pins ------------------------------------------------------


def test_kpi_wire_acknowledge_clock_live_and_dashboard_series_zero(tmp_path):
    definitions = yaml.safe_load(
        (REPO / "platform" / "claim_core" / "sla" / "definitions.yaml").read_text(
            encoding="utf-8")
    )["definitions"]
    acknowledge = next(row for row in definitions if row["id"] == "acknowledge")
    assert acknowledge["status"] == "live"
    assert acknowledge["start_event"] == "claim.created"
    assert acknowledge["stop_event"] == "email.sent"
    assert acknowledge["stop_filter"] == {"template_id": "T-06a"}
    assert acknowledge["calendar"] == "24x7"
    assert acknowledge["escalate_to_role"] == "pending_capture"

    dashboard = yaml.safe_load(
        (MOTOR_PACK / "dashboard.yaml").read_text(encoding="utf-8"))
    series = {row["id"]: row for row in dashboard["series"]}
    assert series["intimation_to_acknowledgement"]["status"] == "live"
    assert series["intimation_to_acknowledgement"]["window"] == "eat_calendar"

    env = _build(tmp_path, "kpi-wire", model=TaskModel({}))
    response = env.client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201
    claim_id = response.json()["id"]
    _drain(env.app)
    clocks = _rows(
        env.app,
        "SELECT state, stopped_at FROM sla_clocks "
        "WHERE claim_id = :c AND definition_id = 'acknowledge'",
        c=claim_id,
    )
    assert len(clocks) == 1 and clocks[0]["stopped_at"] is None

    portfolio = env.client.get("/portfolio", headers=_h(MANAGER))
    assert portfolio.status_code == 200
    tile = next(
        row for row in portfolio.json()["tiles"]
        if row["series_id"] == "intimation_to_acknowledgement"
    )
    # No transport exists before open item 1: the series is live and honestly
    # zero — never a fabricated duration (#145).
    assert tile["status"] == "live"
    assert tile["data"] == {"count": 0, "median_minutes": None}
    csv = env.client.get(
        "/portfolio/intimation_to_acknowledgement.csv", headers=_h(MANAGER))
    assert csv.status_code == 200


def test_intake_pack_pins_checklist_and_money_relevant_slot(tmp_path):
    intake = yaml.safe_load(
        (MOTOR_PACK / "intake" / "intake.yaml").read_text(encoding="utf-8"))
    assert intake["checklist_base_items"] == BASE_CHECKLIST_IDS
    # §5.2 SETTLED/CLOSED money-relevant arm: the slot exists, the type list is
    # uncaptured — pending, never guessed (#148).
    assert intake["money_relevant_doc_types"] == "pending_capture"

    contracts = yaml.safe_load(
        (MOTOR_PACK / "review" / "contracts.yaml").read_text(encoding="utf-8"))
    subtype = contracts["types"]["FIELD_VERIFY"]["subtypes"]["coverage_manual"]
    assert subtype["workspace_layout"] == "coverage_checklist_card"
    assert subtype["resolution_schema"] == "FIELD_VERIFY_COVERAGE@1"
    schema = json.loads(
        (MOTOR_PACK / "review" / "schemas" / "FIELD_VERIFY_COVERAGE@1.json"
         ).read_text(encoding="utf-8"))
    assert "fields" in schema["required"]
    assert "capability_id" in schema["required"], "the #93 correction core survives"

    fields = yaml.safe_load(
        (MOTOR_PACK / "fields.yaml").read_text(encoding="utf-8"))["fields"]
    assert fields["policy.period_start"]["value_type"] == "date"
    assert fields["policy.period_end"]["value_type"] == "date"
    assert fields["policy.endorsement_ref"]["value_type"] == "string"
    assert fields["policy.premium_paid"]["value_type"] == "bool"
    assert fields["policy.excess_protector"]["value_type"] == "bool"


def test_no_new_review_type_no_new_table_no_widened_ceiling(tmp_path):
    contracts = yaml.safe_load(
        (MOTOR_PACK / "review" / "contracts.yaml").read_text(encoding="utf-8"))
    assert len(contracts["types"]) == 17, "PRD-04 enum is FINAL AND CLOSED"

    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in policies["capabilities"]}
    assert by_id["triage.coverage_check"]["max_level"] == "L3"
    assert by_id["triage.decline_draft"]["max_level"] == "L2"
    assert by_id["triage.ex_gratia"]["max_level"] == "L1"

    env = _build(tmp_path, "no-new-table", model=TaskModel({}))
    with env.app.state.engine.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        } if env.app.state.engine.dialect.name == "sqlite" else set()
    if tables:
        assert "agent_runs" in tables
        assert not any(name.startswith("intake_") for name in tables), (
            "PACKET-14 ships no new table"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
