"""PACKET-15 acceptance — PRD-06 document-chase agent: checklists, inbound
matching, reminder engine, hard gates, analytics.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-15_chase_agent.md §3.1. No broker, browser, live Graph
mailbox, network call or live model call is permitted: `email.received`
events are synthetic (register #120), the mailbox classifier is the injected
seam, the PRD-01 pipeline runs on injected fakes, and time is a FixedClock
(all instants fall inside the AR-3a send window). Template bodies stay
`pending_capture` (item 6/#61): every request/reminder/re-request terminates
in a visible staged draft — `chase.reminder_sent` never fires here (#157).
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER_A = "user:01HCHASEOFFICERA000000AAAA"
OFFICER_B = "user:01HCHASEOFFICERB000000AAAA"
MANAGER = "user:01HCHASEMANAGER0000000AAAA"
SELF_ADDRESS = "claims@mayfair.co.ke"
BROKER_ADDR = "broker@abcbrokers.co.ke"

# Monday 2026-07-20 12:00 EAT — inside the AR-3a window; every advance below
# lands Mon–Sat at 12:00 EAT on no fixed-date Kenyan holiday.
T0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

BASE_ITEM_IDS = [
    "claim_form", "logbook_copy", "dl_copy", "kra_pin",
    "police_abstract", "repair_estimate", "photos",
]
SURRENDER_AUTO_ITEMS = {
    "logbook_original", "keys_physical", "kra_pin_cert", "bank_discharge_letter",
}
REGISTRY_ITEM_IDS = {
    *BASE_ITEM_IDS, "incident_description", "logbook_original", "keys_physical",
    "kra_pin_cert", "bank_discharge_letter", "cert_of_incorporation",
}

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

CLAIM_FORM_FIELDS = {
    "fields": [
        {"name": "policy_no", "value": "POL-2026-77", "anchor_text": "POL-2026-77",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
    ]
}

LOGBOOK_FIELDS = {
    "fields": [
        {"name": "reg", "value": "KBX 123A", "anchor_text": "KBX 123A",
         "page": 1, "confidence": 1.0, "citation_mode": "anchor_text"},
    ]
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
        return [{"text": "chase", "bbox": [0.05, 0.05, 0.15, 0.09]}]


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


def _chase_items(app, claim_id: str) -> dict[str, dict]:
    rows = _rows(
        app,
        "SELECT ci.* FROM chase_items ci JOIN chase_checklists cc "
        "ON cc.id = ci.checklist_id WHERE cc.claim_id = :c ORDER BY ci.item_id",
        c=claim_id,
    )
    return {row["item_id"]: row for row in rows}


def _checklists(app, claim_id: str) -> list[dict]:
    return _rows(
        app,
        "SELECT id, claim_id, purpose, status, blocking FROM chase_checklists "
        "WHERE claim_id = :c ORDER BY created_at, id",
        c=claim_id,
    )


def _drafts(app, claim_id: str, capability_id: str) -> list[dict]:
    return [
        item
        for item in _items(app, claim_id=claim_id, type="DRAFT_RELEASE")
        if item["payload"].get("capability_id") == capability_id
    ]


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
    def __init__(self, app, client, classifier, clock) -> None:
        self.app = app
        self.client = client
        self.classifier = classifier
        self.clock = clock
        self.processed: set[str] = set()
        self.message_seq = 0


def _build(tmp_path, name: str, *, model: TaskModel) -> Env:
    from fastapi.testclient import TestClient

    from agent_runtime import build_agent_runtime
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
    return Env(app, TestClient(app), classifier, clock)


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


def _intimation_model(extra: dict[str, list] | None = None) -> TaskModel:
    responses: dict[str, list] = {
        "document_classify": [{"doc_type": "intimation_email", "confidence": 0.99}],
        "extract": [INTIMATION_FIELDS],
    }
    for task, values in (extra or {}).items():
        responses.setdefault(task, []).extend(values)
    return TaskModel(responses)


def _to_checklist(env: Env) -> str:
    """Drive one clean intimation to the S7 chase.init checklist."""

    _set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    _emit_email(env, conversation_id="conv-intake-1", body=BODY_CLEAN,
                subject="New motor claim intimation")
    _advance(env)
    claim_id = _rows(
        env.app, "SELECT id FROM claims ORDER BY created_at, id"
    )[0]["id"]
    _approve_ack(env, claim_id)
    checklists = _checklists(env.app, claim_id)
    assert checklists and checklists[0]["purpose"] == "claim_docs"
    return claim_id


def _reply(env: Env, *, body: str, attachments=()) -> None:
    _emit_email(env, conversation_id="conv-intake-1", body=body,
                attachments=attachments)
    _advance(env)


def _tick(env: Env) -> dict:
    result = env.app.state.chase_agent.tick()
    _drain(env.app)
    return result


def _at(env: Env, days: int, hours: int = 0) -> None:
    env.clock.advance_to(T0 + timedelta(days=days, hours=hours))


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


def _transition(env: Env, claim_id: str, to: str):
    return env.client.post(
        f"/claims/{claim_id}/transition", json={"to": to}, headers=_h(OFFICER_A)
    )


def _walk(env: Env, claim_id: str, states: list[str]) -> None:
    for state in states:
        r = _transition(env, claim_id, state)
        assert r.status_code == 200, f"{state}: {r.text}"


# --- Instantiation (§6.2/§6.3 + registers #159/#160/#165) ---------------------------


def test_chase_init_instantiates_checklist_and_stages_t06_request(tmp_path):
    env = _build(tmp_path, "instantiate", model=_intimation_model())
    claim_id = _to_checklist(env)

    checklist = _checklists(env.app, claim_id)[0]
    assert checklist["status"] == "open"
    assert not checklist["blocking"]
    items = _chase_items(env.app, claim_id)
    assert sorted(items) == sorted(BASE_ITEM_IDS)
    assert all(row["state"] == "requested" for row in items.values())
    assert all(row["requested_at"] is not None for row in items.values())
    assert all(row["reminder_count"] == 0 for row in items.values())
    assert len(_events(env.app, "chase.item_requested", claim_id)) == 7

    # Initial T-06 document request staged at intake.doc_request (#160):
    # pending-capture body renders a visible draft, never invented prose.
    requests = _drafts(env.app, claim_id, "intake.doc_request")
    assert len(requests) == 1
    action_payload = requests[0]["payload"]["action"]["payload"]
    assert action_payload["template_id"] == "T-06"
    assert action_payload["body"] == "pending_capture"
    assert {row["item_id"] for row in action_payload["outstanding"]} == set(
        BASE_ITEM_IDS
    )
    assert action_payload["received"] == []
    assert _events(env.app, "email.sent") == []
    assert _events(env.app, "chase.reminder_sent") == []

    # Per-item doc_item_age clocks are running (#164).
    clocks = _rows(
        env.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :c "
        "AND definition_id = 'doc_item_age' AND stopped_at IS NULL",
        c=claim_id,
    )
    assert len(clocks) == 7

    # Redelivery is a no-op: still one checklist, seven items, one request.
    _advance(env)
    assert len(_checklists(env.app, claim_id)) == 1
    assert len(_drafts(env.app, claim_id, "intake.doc_request")) == 1


# --- Scenario 1 ----------------------------------------------------------------------


def test_scenario_1_three_docs_verified_reminder_lists_outstanding_four(tmp_path):
    model = _intimation_model({
        "document_classify": [
            {"doc_type": "claim_form", "confidence": 0.99},
            {"doc_type": "logbook", "confidence": 0.99},
            {"doc_type": "driving_licence", "confidence": 0.99},
        ],
        "extract": [CLAIM_FORM_FIELDS, LOGBOOK_FIELDS, {"fields": []}],
    })
    env = _build(tmp_path, "scenario-1", model=model)
    claim_id = _to_checklist(env)

    _reply(env, body="Claim form attached",
           attachments=(("form.pdf", "application/pdf",
                         _pdf(["Claim form. Policy POL-2026-77"])),))
    _reply(env, body="Logbook attached",
           attachments=(("logbook.pdf", "application/pdf",
                         _pdf(["Logbook for KBX 123A"])),))
    _reply(env, body="Driving licence attached",
           attachments=(("licence.pdf", "application/pdf",
                         _pdf(["Driving licence scan"])),))

    items = _chase_items(env.app, claim_id)
    assert items["claim_form"]["state"] == "verified"
    assert items["logbook_copy"]["state"] == "verified"
    assert items["dl_copy"]["state"] == "verified"
    assert items["claim_form"]["document_id"]
    verified_events = _events(env.app, "chase.item_verified", claim_id)
    assert {event["payload"]["item_id"] for event in verified_events} == {
        "claim_form", "logbook_copy", "dl_copy",
    }

    # The three matched items' age clocks stopped; four still run (#164).
    open_clocks = _rows(
        env.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :c "
        "AND definition_id = 'doc_item_age' AND stopped_at IS NULL",
        c=claim_id,
    )
    assert len(open_clocks) == 4

    _at(env, days=3, hours=1)
    result = _tick(env)
    assert result["sent"] == 1

    reminders = _drafts(env.app, claim_id, "chase.reminder")
    assert len(reminders) == 1
    payload = reminders[0]["payload"]["action"]["payload"]
    assert payload["template_id"] == "T-06r-broker"
    assert {row["item_id"] for row in payload["outstanding"]} == {
        "kra_pin", "police_abstract", "repair_estimate", "photos",
    }
    assert all(row["age_days"] == 3 for row in payload["outstanding"])
    assert set(payload["received"]) == {"claim_form", "logbook_copy", "dl_copy"}
    items = _chase_items(env.app, claim_id)
    assert items["kra_pin"]["reminder_count"] == 1
    assert items["claim_form"]["reminder_count"] == 0
    assert _events(env.app, "email.sent") == []

    # Same instant, tick again: nothing newly due — no duplicate reminder.
    assert _tick(env)["sent"] == 0
    assert len(_drafts(env.app, claim_id, "chase.reminder")) == 1


# --- Scenario 2 ----------------------------------------------------------------------


def test_scenario_2_illegible_logbook_rejected_with_defect_rerequest(tmp_path):
    model = _intimation_model({
        "document_classify": [{"doc_type": "logbook", "confidence": 0.99}],
        "extract": [{"fields": []}],
    })
    env = _build(tmp_path, "scenario-2", model=model)
    claim_id = _to_checklist(env)

    _reply(env, body="Logbook attached",
           attachments=(("logbook.pdf", "application/pdf",
                         _pdf(["blurry unreadable scan"])),))

    item = _chase_items(env.app, claim_id)["logbook_copy"]
    assert item["state"] == "rejected"
    assert item["reject_reason"] == "illegible"
    rejected = _events(env.app, "chase.item_rejected", claim_id)
    assert rejected and rejected[-1]["payload"]["item_id"] == "logbook_copy"

    rerequests = _drafts(env.app, claim_id, "chase.rerequest")
    assert len(rerequests) == 1
    payload = rerequests[0]["payload"]["action"]["payload"]
    assert payload["template_id"] == "T-06r-broker"
    assert payload["defect"] == {"item_id": "logbook_copy", "reason": "illegible"}
    assert payload["body"] == "pending_capture"
    assert _events(env.app, "email.sent") == []


# --- Scenario 3 ----------------------------------------------------------------------


def test_scenario_3_declined_mid_chase_sends_zero_further_reminders(tmp_path):
    env = _build(tmp_path, "scenario-3", model=_intimation_model())
    claim_id = _to_checklist(env)

    # Complete triage (no estimate: R-02 stays visibly blocked — #149) and
    # decline from TRIAGED (PRD-00 §0.4 standard path).
    card = next(
        item for item in _items(env.app, claim_id=claim_id, type="FIELD_VERIFY")
        if item["subtype"] == "coverage_manual" and item["status"] == "open"
    )
    response = _resolve(
        env, card["id"], OFFICER_A, action="approve",
        schema_version="FIELD_VERIFY_COVERAGE@1",
        payload={
            "capability_id": "triage.coverage_check",
            "diff": _diff(),
            "fields": {
                "policy.sum_insured": 600_000_00,
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
    declined = env.client.post(
        f"/claims/{claim_id}/decline",
        json={"reason": "below_excess"},
        headers=_h(OFFICER_A),
    )
    assert declined.status_code == 200, declined.text
    _drain(env.app)

    checklist = _checklists(env.app, claim_id)[0]
    assert checklist["status"] == "cancelled"
    assert _events(env.app, "chase.cancelled", claim_id)

    _at(env, days=3, hours=1)
    result = _tick(env)
    assert result["sent"] == 0
    assert _drafts(env.app, claim_id, "chase.reminder") == []
    _at(env, days=10)
    assert _tick(env)["sent"] == 0
    assert _drafts(env.app, claim_id, "chase.reminder") == []


# --- Scenario 4: surrender hard gate --------------------------------------------------


def test_scenario_4_surrender_gate_blocks_settlement_until_attested(tmp_path):
    env = _build(tmp_path, "scenario-4", model=TaskModel({}))
    response = env.client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201
    claim_id = response.json()["id"]
    _write(env, claim_id, {
        "assessment.agreed_quote": 600_000_00,
        "assessment.pav": 1_000_000_00,
        "policy.sum_insured": 1_200_000_00,
        "logbook.bank_interest.present": True,
    })
    _walk(env, claim_id, [
        "TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED",
        "WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION", "SURRENDER_CHECKLIST",
    ])
    _drain(env.app)

    checklists = _checklists(env.app, claim_id)
    surrender = next(row for row in checklists if row["purpose"] == "surrender")
    assert surrender["blocking"]
    items = _chase_items(env.app, claim_id)
    assert set(items) == SURRENDER_AUTO_ITEMS  # discharge letter auto-present (§6.5)
    assert items["logbook_original"]["physical"]
    assert items["keys_physical"]["physical"]
    # No outbound request for a surrender checklist (physical/officer-driven).
    assert _drafts(env.app, claim_id, "intake.doc_request") == []

    blocked = _transition(env, claim_id, "SETTLEMENT")
    assert blocked.status_code == 409, blocked.text
    blocked_on = " ".join(blocked.json().get("blocked_on", []))
    assert "R-13" in blocked_on and "R-14" in blocked_on

    # Attest is physical-only.
    non_physical = env.client.post(
        f"/chase/items/{items['kra_pin_cert']['id']}/attest",
        json={}, headers=_h(OFFICER_A),
    )
    assert non_physical.status_code == 422

    for item_id in ("logbook_original", "keys_physical"):
        attested = env.client.post(
            f"/chase/items/{items[item_id]['id']}/attest",
            json={}, headers=_h(OFFICER_A),
        )
        assert attested.status_code == 200, attested.text
    _drain(env.app)

    hydrated = env.client.get(f"/claims/{claim_id}", headers=_h(OFFICER_A)).json()
    assert hydrated["fields"]["salvage.logbook_held"]["value"] is True
    assert hydrated["fields"]["salvage.keys_held"]["value"] is True
    assert hydrated["fields"]["salvage.logbook_held"]["source_type"] == "human"

    # R-13 clears from real attestations; R-14 stays blocked on #49.
    still_blocked = _transition(env, claim_id, "SETTLEMENT")
    assert still_blocked.status_code == 409
    blocked_on = " ".join(still_blocked.json().get("blocked_on", []))
    assert "R-14" in blocked_on and "R-13" not in blocked_on

    # Waivers: blocking items are claims_manager-only, reason mandatory, ledgered.
    discharge = items["bank_discharge_letter"]["id"]
    assert env.client.post(
        f"/chase/items/{discharge}/waive",
        json={"reason": "bank confirmed nil interest by phone"},
        headers=_h(OFFICER_A),
    ).status_code == 403
    assert env.client.post(
        f"/chase/items/{discharge}/waive", json={}, headers=_h(MANAGER)
    ).status_code == 422
    waived = env.client.post(
        f"/chase/items/{discharge}/waive",
        json={"reason": "bank confirmed nil interest by phone"},
        headers=_h(MANAGER),
    )
    assert waived.status_code == 200, waived.text
    _drain(env.app)
    row = _chase_items(env.app, claim_id)["bank_discharge_letter"]
    assert row["state"] == "waived"
    assert row["waived_by"] == MANAGER
    assert row["waiver_reason"]
    ledger_actions = [
        r["action"] for r in _rows(env.app, "SELECT action FROM audit_ledger")
    ]
    assert "chase.item_waived" in ledger_actions
    assert "chase.item_received" in ledger_actions


# --- Scenario 5: cadence, ladder, deferral, cap ---------------------------------------


def test_scenario_5_cadence_ladder_deferral_and_cap_escalation(tmp_path):
    env = _build(tmp_path, "scenario-5", model=_intimation_model())
    claim_id = _to_checklist(env)
    insured_party = _rows(
        env.app,
        "SELECT id FROM parties WHERE claim_id = :c AND role = 'insured'",
        c=claim_id,
    )[0]["id"]

    # Reminder 1 at T+3d: requester only.
    _at(env, days=3, hours=1)
    assert _tick(env)["sent"] == 1
    first = _drafts(env.app, claim_id, "chase.reminder")[0]
    assert insured_party not in first["payload"]["action"]["payload"]["to_party_ids"]

    # Inbound reply within 24h of the next due tick defers the whole checklist
    # 48h (§6.4 v1.1, #172) — a human just engaged.
    _at(env, days=7, hours=-1)
    _reply(env, body="Thanks, we are gathering the remaining documents")
    _at(env, days=7, hours=1)
    deferred = _tick(env)
    assert deferred["sent"] == 0
    assert deferred["deferred"] >= 1
    assert len(_drafts(env.app, claim_id, "chase.reminder")) == 1

    # Past the deferral window the reminder goes out; insured joins from
    # reminder 2 (#168 recipient ladder).
    _at(env, days=9, hours=2)
    assert _tick(env)["sent"] == 1
    second = _drafts(env.app, claim_id, "chase.reminder")[1]
    assert insured_party in second["payload"]["action"]["payload"]["to_party_ids"]

    # Walk the remaining cadence to the cap of six.
    for days in (12, 19, 26, 33):
        _at(env, days=days, hours=1)
        assert _tick(env)["sent"] == 1
    assert len(_drafts(env.app, claim_id, "chase.reminder")) == 6
    items = _chase_items(env.app, claim_id)
    assert all(items[item_id]["reminder_count"] == 6 for item_id in BASE_ITEM_IDS)

    # Beyond the cap: no seventh reminder — one idempotent escalation item.
    _at(env, days=40, hours=1)
    result = _tick(env)
    assert result["sent"] == 0
    assert result["escalated"] >= 1
    assert len(_drafts(env.app, claim_id, "chase.reminder")) == 6
    exhausted = _items(
        env.app, claim_id=claim_id, type="EXCEPTION", subtype="chase_exhausted"
    )
    assert len(exhausted) == 1
    assert set(exhausted[0]["payload"]["items"]) == set(BASE_ITEM_IDS)
    _at(env, days=47, hours=1)
    _tick(env)
    assert len(_items(
        env.app, claim_id=claim_id, type="EXCEPTION", subtype="chase_exhausted"
    )) == 1
    assert _events(env.app, "email.sent") == []
    assert _events(env.app, "chase.reminder_sent") == []


# --- Scenario 6: analytics ------------------------------------------------------------


def test_scenario_6_analytics_series_return_non_null_medians(tmp_path):
    model = _intimation_model({
        "document_classify": [{"doc_type": "claim_form", "confidence": 0.99}],
        "extract": [CLAIM_FORM_FIELDS],
    })
    env = _build(tmp_path, "scenario-6", model=model)
    claim_id = _to_checklist(env)
    _at(env, days=1, hours=2)
    _reply(env, body="Claim form attached",
           attachments=(("form.pdf", "application/pdf",
                         _pdf(["Claim form. Policy POL-2026-77"])),))
    assert _chase_items(env.app, claim_id)["claim_form"]["state"] == "verified"

    portfolio = env.client.get("/portfolio", headers=_h(MANAGER))
    assert portfolio.status_code == 200
    tiles = {row["series_id"]: row for row in portfolio.json()["tiles"]}
    for series_id in ("chase_doc_type_cycle", "chase_broker_league",
                      "chase_cycle_time"):
        assert tiles[series_id]["status"] == "live"

    cycle = tiles["chase_doc_type_cycle"]["data"]
    assert cycle["claim_form"]["median_minutes"] is not None
    assert cycle["claim_form"]["median_minutes"] > 0
    assert cycle["claim_form"]["count"] == 1
    league = tiles["chase_broker_league"]["data"]
    assert league, "seeded history must produce a broker league row"

    for series_id in ("chase_doc_type_cycle", "chase_broker_league",
                      "chase_cycle_time"):
        csv = env.client.get(f"/portfolio/{series_id}.csv", headers=_h(MANAGER))
        assert csv.status_code == 200


# --- Pack pins, registry, enums -------------------------------------------------------


def test_items_registry_chase_config_and_pack_pins(tmp_path):
    registry = yaml.safe_load(
        (MOTOR_PACK / "checklists" / "items.yaml").read_text(encoding="utf-8"))
    assert registry["version"] == 1
    items = registry["items"]
    assert set(items) == REGISTRY_ITEM_IDS
    assert items["incident_description"]["kind"] == "field_request"
    assert items["incident_description"]["target_path"] == "loss.narrative"
    assert items["logbook_original"]["kind"] == "physical"
    assert items["logbook_original"]["physical"] is True
    assert items["keys_physical"]["physical"] is True
    assert items["logbook_copy"]["kind"] == "document"
    assert items["logbook_copy"]["doc_type"] == "logbook"
    assert items["photos"]["doc_type"] == "photo_damage"
    assert items["bank_discharge_letter"]["doc_type"] == "bank_discharge_letter"

    # One doc-type registry: the PACKET-14 intake map is superseded (#165).
    intake = yaml.safe_load(
        (MOTOR_PACK / "intake" / "intake.yaml").read_text(encoding="utf-8"))
    assert "checklist_doc_types" not in intake
    assert intake["checklist_base_items"] == BASE_ITEM_IDS

    chase = yaml.safe_load(
        (MOTOR_PACK / "chase" / "chase.yaml").read_text(encoding="utf-8"))
    assert chase["version"] == 1
    assert chase["cadence_days"] == [3, 7, 12]
    assert chase["repeat_days"] == 7
    assert chase["reminder_cap"] == 6
    assert chase["inbound_defer"] == {"window_hours": 24, "defer_hours": 48}
    assert chase["cc_insured_from_reminder"] == 2
    assert set(chase["reject_reasons"]) <= {
        "illegible", "wrong_vehicle", "expired", "wrong_document",
    }

    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in policies["capabilities"]}
    assert by_id["chase.reminder"]["max_level"] == "L4"
    assert by_id["chase.reminder"]["initial_level"] == "L1"
    assert by_id["chase.rerequest"]["max_level"] == "L4"
    assert by_id["chase.rerequest"]["initial_level"] == "L1"

    definitions = yaml.safe_load(
        (REPO / "platform" / "claim_core" / "sla" / "definitions.yaml").read_text(
            encoding="utf-8")
    )["definitions"]
    doc_item_age = next(row for row in definitions if row["id"] == "doc_item_age")
    assert doc_item_age["status"] == "live"
    assert doc_item_age["start_event"] == "chase.item_requested"
    assert doc_item_age["stop_event"] == "chase.item_received"
    assert doc_item_age["key_field"] == "chase_item_id"
    assert doc_item_age["escalate_to_role"] == "pending_capture"
    assert doc_item_age["calendar"] == "send_window"

    fields = yaml.safe_load(
        (MOTOR_PACK / "fields.yaml").read_text(encoding="utf-8"))["fields"]
    assert fields["logbook.bank_interest.present"]["value_type"] == "bool"

    dashboard = yaml.safe_load(
        (MOTOR_PACK / "dashboard.yaml").read_text(encoding="utf-8"))
    series = {row["id"]: row for row in dashboard["series"]}
    for series_id in ("chase_doc_type_cycle", "chase_broker_league",
                      "chase_cycle_time"):
        assert series[series_id]["status"] == "live"

    from claim_core.ledger import ACTION_MAP

    assert {
        "chase.item_requested", "chase.item_received", "chase.item_verified",
        "chase.item_rejected", "chase.item_waived", "chase.item_snoozed",
        "chase.complete", "chase.cancelled",
    } <= set(ACTION_MAP)

    contracts = yaml.safe_load(
        (MOTOR_PACK / "review" / "contracts.yaml").read_text(encoding="utf-8"))
    assert len(contracts["types"]) == 17, "PRD-04 enum stays FINAL AND CLOSED"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
