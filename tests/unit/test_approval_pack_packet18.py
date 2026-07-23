"""PACKET-18 regression tests for the CTO review findings.

These cover the invariants the protected acceptance suite does not exercise:
PRD-08 §1.2 authorisation, §1.5 request idempotency and version allocation,
PRD-03 §3.3 critical-grader gating, AR-4 budget enforcement, §1.9 single open
review, and the §1.4/#227 render-timestamp contract.
"""
from __future__ import annotations

import io
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

OFFICER = "user:01HPACKOFFICER0000000AAAAZ"
MANAGER = "user:01HPACKMANAGER0000000AAAAZ"
APPROVER = "user:01HPACKAPPROVER00000AAAAZZ"
OUTSIDER = "user:01HPACKOUTSIDER000000AAAZZ"
T0 = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)

ESTIMATE = 26_100_000
ASSESSED = 13_627_600
PAV = 150_000_000
SUM_INSURED = 60_000_000
EXCESS = 1_500_000

ACTIVE_FIELDS = {
    "vehicle.reg": ("KBX 123A", "string"),
    "loss.date": ("2026-07-01", "date"),
    "loss.location": ("Mombasa Road", "string"),
    "loss.narrative": ("The insured vehicle collided at a junction.", "string"),
    "assessment.estimate_total": (ESTIMATE, "money"),
    "assessment.agreed_quote": (ASSESSED, "money"),
    "assessment.pav": (PAV, "money"),
    "policy.sum_insured": (SUM_INSURED, "money"),
    "policy.excess_amount": (EXCESS, "money"),
    "policy.excess_protector": (False, "bool"),
}


class FixedClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0, days: int = 0) -> None:
        self.now += timedelta(seconds=seconds, days=days)


def _pdf(*pages: str) -> bytes:
    import fitz

    document = fitz.open()
    for value in pages:
        document.new_page().insert_text((72, 72), value)
    result = document.tobytes(garbage=4, deflate=True)
    document.close()
    return result


def _png(colour: str) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(output, format="PNG")
    return output.getvalue()


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def render(self, html: str, *, policy: Any) -> Any:
        self.calls.append(html)
        return SimpleNamespace(
            pdf_bytes=_pdf("HTML"),
            fallback_used=False,
            fallback_reason=None,
            blocked_resource_count=0,
            chromium_version="fake-pinned-chromium",
        )


class TaskModel:
    def __init__(self, responses: dict[str, list[dict[str, Any]]], cost_usd: float = 0.01) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[dict[str, Any]] = []
        self.cost_usd = cost_usd

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        task = str(inputs.get("task"))
        self.calls.append({"tier": tier, "inputs": dict(inputs)})
        queue = self.responses.get(task, [])
        if not queue:
            raise AssertionError(f"unscripted model task: {task!r}")
        return {
            "data": queue.pop(0),
            "cost_usd": self.cost_usd,
            "model_id": f"fake-{tier.casefold()}",
        }


def _commentary() -> dict[str, Any]:
    return {
        "paragraphs": [
            {
                "template_slot": "incident_summary",
                "content": "The insured reported a collision on Mombasa Road.",
                "numbers_used": [],
            },
            {
                "template_slot": "excess_vs_max",
                "content": "The assessed amount is KES 136,276.",
                "numbers_used": ["136,276"],
            },
            {
                "template_slot": "savings_narrative",
                "content": "The estimate was KES 261,000.",
                "numbers_used": ["261,000"],
            },
        ]
    }


def _g_note_clean() -> dict[str, Any]:
    return {
        "numeric_claims": [
            {
                "text": "136,276",
                "source_kind": "claim_field",
                "source_ref": "assessment.agreed_quote",
                "observed_value": ASSESSED,
                "value_type": "money",
            },
            {
                "text": "261,000",
                "source_kind": "claim_field",
                "source_ref": "assessment.estimate_total",
                "observed_value": ESTIMATE,
                "value_type": "money",
            },
        ],
        "unsupported_assertions": [],
        "missing_sections": [],
        "tone_ok": True,
    }


def _model(*, commentary: list | None = None, gnote: list | None = None,
           cost_usd: float = 0.01) -> TaskModel:
    return TaskModel(
        {
            "pack_note_commentary": commentary if commentary is not None else [_commentary()],
            "g_note_grade": gnote if gnote is not None else [_g_note_clean()],
        },
        cost_usd=cost_usd,
    )


class Env:
    def __init__(self, app, client, clock, renderer, model) -> None:
        self.app = app
        self.client = client
        self.clock = clock
        self.renderer = renderer
        self.model = model

    @property
    def service(self):
        return self.app.state.approval_pack_agent


def _build(tmp_path: pathlib.Path, name: str, *, model: TaskModel,
           renderer: FakeRenderer | None = None) -> Env:
    from fastapi.testclient import TestClient

    from agent_runtime import build_agent_runtime
    from approval_pack_agent import build_approval_pack_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    clock = FixedClock()
    renderer = renderer or FakeRenderer()
    app = create_app(f"sqlite:///{tmp_path}/{name}.db", clock=clock)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app, model_client=model)
    build_review_queue(
        app,
        roles={
            OFFICER: "claims_officer",
            MANAGER: "claims_manager",
            APPROVER: "gm",
            OUTSIDER: "auditor",
        },
    )
    build_agent_runtime(app)
    build_approval_pack_agent(app, model_client=model, html_renderer=renderer)
    return Env(app, TestClient(app), clock, renderer, model)


def _h(actor: str = OFFICER) -> dict[str, str]:
    return {"X-Actor": actor}


def _rows(app, sql: str, **params) -> list[dict[str, Any]]:
    with app.state.engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(sql), params).mappings()]


def _events(app, type_: str, claim_id: str) -> list[dict[str, Any]]:
    return _rows(
        app,
        "SELECT id, payload FROM events WHERE type = :type AND claim_id = :claim_id ORDER BY seq",
        type=type_,
        claim_id=claim_id,
    )


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row["payload"]
    return json.loads(value) if isinstance(value, str) else value


def _reviews(app, claim_id: str, type_: str) -> list[dict[str, Any]]:
    return _rows(
        app,
        "SELECT id, subtype, status, payload FROM review_items "
        "WHERE claim_id = :claim_id AND type = :type ORDER BY created_at, id",
        claim_id=claim_id,
        type=type_,
    )


def _set_level(env: Env, capability_id: str, level: str) -> None:
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
            {"level": level, "id": capability_id},
        )


def _seed(env: Env, suffix: str = "A") -> dict[str, Any]:
    from sqlalchemy.orm import Session

    response = env.client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.0.0"}, headers=_h()
    )
    claim_id = response.json()["id"]
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET status = 'RESERVED' WHERE id = :claim_id"),
            {"claim_id": claim_id},
        )
        connection.execute(
            text(
                "INSERT INTO chase_checklists (id, claim_id, purpose, status, blocking, "
                "requester_party_id, created_at) VALUES (:id, :claim_id, 'claim_docs', "
                "'complete', true, NULL, :created_at)"
            ),
            {"id": f"CL-{suffix}", "claim_id": claim_id, "created_at": T0},
        )
    env.client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": value,
                    "value_type": value_type,
                    "source_type": "human",
                    "source_ref": {"user_id": OFFICER},
                    "verification_state": "human_verified",
                }
                for path, (value, value_type) in ACTIVE_FIELDS.items()
            ]
        },
        headers=_h(),
    )

    def add_document(filename: str, content: bytes, doc_type: str | None,
                     mime: str = "application/pdf") -> str:
        row = env.app.state.claim_service.add_document(
            claim_id,
            filename=filename,
            mime=mime,
            content=content,
            source_channel="test_fixture",
            source_ref=f"src-{filename}",
            actor=OFFICER,
        )
        with env.app.state.engine.begin() as connection:
            connection.execute(
                text("UPDATE documents SET doc_type = :t, status = 'verified' WHERE id = :id"),
                {"t": doc_type, "id": row.id},
            )
        return row.id

    documents = {
        "policy": add_document("policy.pdf", _pdf("POLICY"), None),
        "claim_form": add_document("claim-form.pdf", _pdf("CLAIM"), "claim_form"),
        "logbook": add_document("logbook.pdf", _pdf("LOGBOOK"), "logbook"),
        "licence": add_document("licence.pdf", _pdf("LICENCE"), "driving_licence"),
        "kra": add_document("kra.pdf", _pdf("KRA"), "kra_pin_cert"),
        "photo": add_document("damage.png", _png("red"), "photo_damage", "image/png"),
        "estimate": add_document("estimate.pdf", _pdf("ESTIMATE"), "repair_estimate"),
        "report": add_document("report.pdf", _pdf("REPORT"), "assessor_report"),
        "quote": add_document("quote.pdf", _pdf("QUOTE"), None),
    }

    def add_communication(purpose: str, sequence: int) -> str:
        row, _created = env.app.state.claim_service.record_inbound_communication(
            graph_message_id=f"msg-{suffix}-{sequence}",
            claim_id=claim_id,
            thread_id=f"thread-{claim_id}",
            from_addr="broker@example.co.ke",
            to_addrs=["claims@mayfair.co.ke"],
            subject=purpose,
            body_text=f"<html><body><p>{purpose}</p></body></html>",
        )
        return row.id

    communications = {
        "intimation": add_communication("Intimation", 1),
        "engagement": add_communication("Engagement", 2),
    }
    with Session(env.app.state.engine) as session:
        env.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="assessment.report_received",
            payload={"claim_id": claim_id, "document_id": documents["report"],
                     "assessor_party_id": "PARTY-A"},
            actor="agent:assessment",
            correlation_id=None,
        )
        session.commit()

    def select(item_id: str, sources: list[dict[str, str]]) -> None:
        response = env.client.put(
            f"/claims/{claim_id}/approval-pack/manifest/{item_id}/sources",
            json={"sources": sources},
            headers=_h(),
        )
        assert response.status_code == 200, response.text

    select("policy_document", [{"kind": "document", "id": documents["policy"]}])
    select("intimation_email", [{"kind": "communication", "id": communications["intimation"]}])
    select("assessor_engagement_email",
           [{"kind": "communication", "id": communications["engagement"]}])
    select("supplier_quotes", [{"kind": "document", "id": documents["quote"]}])
    for item_id in ("assessor_payment_request", "claim_details_report"):
        upload = env.client.post(
            f"/claims/{claim_id}/approval-pack/manifest/{item_id}/upload",
            files={"file": (f"{item_id}.pdf", _pdf(item_id), "application/pdf")},
            headers=_h(),
        )
        assert upload.status_code == 201, upload.text
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO savings_ledger (id, claim_id, kind, baseline_amount, "
                "achieved_amount, evidence, vendor_id, occurred_at) VALUES "
                "(:id, :claim_id, 'assessment_negotiation', :baseline, :achieved, "
                ":evidence, NULL, :occurred_at)"
            ),
            {
                "id": f"SAVE-{suffix}",
                "claim_id": claim_id,
                "baseline": ESTIMATE,
                "achieved": ASSESSED,
                "evidence": json.dumps({"citations": [{"document_id": documents["report"]}]}),
                "occurred_at": T0,
            },
        )
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h())
    assert card.status_code == 200, card.text
    assert card.json()["ready"] is True, card.json()["blockers"]
    return {"claim_id": claim_id, "readiness": card.json(), "documents": documents}


def _generate(env: Env, seeded: dict[str, Any], key: str = "gen-1"):
    return env.client.post(
        f"/claims/{seeded['claim_id']}/approval-pack/generate",
        json={"readiness_fingerprint": seeded["readiness"]["fingerprint"]},
        headers={**_h(), "Idempotency-Key": key},
    )


# --- §1.2 authorisation --------------------------------------------------------------


@pytest.mark.parametrize("actor", [APPROVER, OUTSIDER])
def test_only_officer_and_manager_may_write_to_the_approval_pack(tmp_path, actor):
    env = _build(tmp_path, "roles", model=_model())
    seeded = _seed(env, "R")
    claim_id = seeded["claim_id"]

    denied = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "document", "id": seeded["documents"]["policy"]}]},
        headers=_h(actor),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_ROLE"

    upload = env.client.post(
        f"/claims/{claim_id}/approval-pack/manifest/claim_details_report/upload",
        files={"file": ("x.pdf", _pdf("X"), "application/pdf")},
        headers=_h(actor),
    )
    assert upload.status_code == 403

    generated = env.client.post(
        f"/claims/{claim_id}/approval-pack/generate",
        json={"readiness_fingerprint": seeded["readiness"]["fingerprint"]},
        headers={**_h(actor), "Idempotency-Key": "denied"},
    )
    assert generated.status_code == 403
    assert _events(env.app, "pack.merge_requested", claim_id) == []


def test_claims_manager_may_write_and_approver_may_only_read(tmp_path):
    env = _build(tmp_path, "roles-read", model=_model())
    seeded = _seed(env, "RR")
    claim_id = seeded["claim_id"]

    allowed = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "document", "id": seeded["documents"]["policy"]}]},
        headers=_h(MANAGER),
    )
    assert allowed.status_code == 200

    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h(APPROVER))
    assert card.status_code == 200
    blocked = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h(OUTSIDER))
    assert blocked.status_code == 403


# --- §1.5 idempotency and version allocation ------------------------------------------


def test_repeated_request_key_appends_one_request_event_and_one_staged_action(tmp_path):
    env = _build(tmp_path, "idem", model=_model())
    seeded = _seed(env, "I")
    claim_id = seeded["claim_id"]

    first = _generate(env, seeded, key="stable")
    second = _generate(env, seeded, key="stable")
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["review_item_id"] == second.json()["review_item_id"]
    assert len(_events(env.app, "pack.merge_requested", claim_id)) == 1
    staged = [
        item
        for item in _reviews(env.app, claim_id, "DRAFT_RELEASE")
        if _payload(item).get("capability_id") == "pack.merge"
    ]
    assert len(staged) == 1


def test_concurrent_identical_source_selection_appends_once(tmp_path):
    env = _build(tmp_path, "source-idem", model=_model())
    seeded = _seed(env, "SI")
    claim_id = seeded["claim_id"]
    sources = [{"kind": "document", "id": seeded["documents"]["quote"]}]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: env.service.select_sources(
                    claim_id, "policy_document", sources, OFFICER
                ),
                range(2),
            )
        )

    assert sorted(result["recorded"] for result in results) == [False, True]
    selections = [
        _payload(event)
        for event in _events(env.app, "pack.sources_selected", claim_id)
        if _payload(event).get("item_id") == "policy_document"
    ]
    assert len(selections) == 2  # initial seed plus one replacement


def test_generation_recomputes_readiness_while_holding_the_claim_lock(tmp_path):
    env = _build(tmp_path, "readiness-lock", model=_model())
    seeded = _seed(env, "RL")
    original_transaction = env.service._claim_transaction
    original_evaluate = env.service.readiness.evaluate
    state = {"inside": False, "observed": False}

    @contextmanager
    def tracked_transaction(claim_id):
        with original_transaction(claim_id) as session:
            state["inside"] = True
            try:
                yield session
            finally:
                state["inside"] = False

    def tracked_evaluate(claim_id, actor):
        state["observed"] = state["observed"] or state["inside"]
        return original_evaluate(claim_id, actor)

    env.service._claim_transaction = tracked_transaction
    env.service.readiness.evaluate = tracked_evaluate
    response = _generate(env, seeded, key="locked-read")
    assert response.status_code == 202
    assert state == {"inside": False, "observed": True}


def test_version_allocation_is_serialised_under_the_claim_lock(tmp_path):
    env = _build(tmp_path, "versions", model=_model(commentary=[], gnote=[]))
    seeded = _seed(env, "V")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")

    assert _generate(env, seeded, key="v1").status_code == 201
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h())
    seeded["readiness"] = card.json()
    assert _generate(env, seeded, key="v2").status_code == 201

    decoded = [_payload(event) for event in _events(env.app, "pack.merged", claim_id)]
    assert [payload["version"] for payload in decoded] == [1, 2]
    assert len({payload["blob_key"] for payload in decoded}) == 2


def test_duplicate_completed_merge_is_a_noop_after_readiness_changes(tmp_path):
    from agent_runtime import Action

    env = _build(tmp_path, "merge-replay", model=_model())
    seeded = _seed(env, "MR")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    assert _generate(env, seeded, key="original").status_code == 201

    request = _events(env.app, "pack.merge_requested", claim_id)[0]
    request_payload = _payload(request)
    env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "document", "id": seeded["documents"]["quote"]}]},
        headers=_h(),
    )
    env.service.execute_merge(
        Action(
            type="pack.merge",
            payload={
                "claim_id": claim_id,
                "request_event_id": request["id"],
                "readiness_fingerprint": request_payload["readiness_fingerprint"],
                "actor": OFFICER,
            },
        )
    )
    assert len(_events(env.app, "pack.merged", claim_id)) == 1
    assert _events(env.app, "pack.generation_refused", claim_id) == []


# --- PRD-03 §3.3 critical grader gating ----------------------------------------------


def test_failed_merged_pack_grade_blocks_the_note_and_the_event_index(tmp_path):
    env = _build(tmp_path, "gate", model=_model())
    seeded = _seed(env, "G")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    harness = env.app.state.eval_harness
    original = harness.grade

    def failing(grader_id, subject_ref, actor):
        if subject_ref.get("artifact_kind") == "merged_pack":
            return SimpleNamespace(
                grader_id="G-TPL",
                subject_type="artifact",
                result="fail",
                severity="critical",
                detail={"code": "PACK_DIGEST_MISMATCH"},
                grader_run_id="RUN-FAKE",
            )
        return original(grader_id, subject_ref, actor)

    harness.grade = failing
    response = _generate(env, seeded)
    assert response.status_code == 409
    assert response.json()["code"] == "PACK_GENERATION_BLOCKED"
    assert response.json()["subtypes"] == ["pack_integrity_failed"]

    assert _events(env.app, "pack.merged", claim_id) == []
    assert _rows(env.app, "SELECT id FROM note_drafts WHERE claim_id = :c", c=claim_id) == []
    assert _reviews(env.app, claim_id, "NOTE_REVIEW") == []
    exceptions = [
        item
        for item in _reviews(env.app, claim_id, "EXCEPTION")
        if item["subtype"] == "pack_integrity_failed"
    ]
    assert len(exceptions) == 1
    claim = env.client.get(f"/claims/{claim_id}", headers=_h())
    assert claim.json()["status"] == "RESERVED"


# --- AR-4 budgets ---------------------------------------------------------------------


def test_regeneration_shares_one_per_call_budget_ceiling(tmp_path):
    bad = {
        "paragraphs": [
            {"template_slot": "incident_summary",
             "content": "The vehicle travelled at 999 kilometres per hour.",
             "numbers_used": ["999"]},
            {"template_slot": "excess_vs_max", "content": "No comment.", "numbers_used": []},
            {"template_slot": "savings_narrative", "content": "No comment.", "numbers_used": []},
        ]
    }
    # One attempt already reaches the configured 0.18 per-call ceiling, so the
    # regeneration must be refused. A per-attempt wrapper would allow it.
    env = _build(tmp_path, "budget", model=_model(commentary=[bad, bad], gnote=[], cost_usd=0.18))
    seeded = _seed(env, "B")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    _generate(env, seeded)
    commentary_calls = [
        call for call in env.model.calls
        if call["inputs"].get("task") == "pack_note_commentary"
    ]
    assert len(commentary_calls) == 1
    assert _rows(env.app, "SELECT id FROM note_drafts WHERE claim_id = :c", c=claim_id) == []
    subtypes = {item["subtype"] for item in _reviews(env.app, claim_id, "EXCEPTION")}
    assert "budget_exceeded" in subtypes
    assert _reviews(env.app, claim_id, "NOTE_REVIEW") == []


def test_claim_lifetime_budget_refuses_before_the_call(tmp_path):
    env = _build(tmp_path, "budget-life", model=_model())
    seeded = _seed(env, "L")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    env.service.config.commentary["claim_lifetime_budget_usd"] = 0.0

    _generate(env, seeded)
    assert [
        call for call in env.model.calls
        if call["inputs"].get("task") == "pack_note_commentary"
    ] == []
    subtypes = {item["subtype"] for item in _reviews(env.app, claim_id, "EXCEPTION")}
    assert "budget_exceeded" in subtypes


def test_single_call_over_max_cost_is_recorded_and_blocked(tmp_path):
    env = _build(tmp_path, "budget-single", model=_model(cost_usd=0.19))
    seeded = _seed(env, "BS")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    response = _generate(env, seeded)
    assert response.status_code == 201
    assert response.json()["note_status"] == "blocked_on_exception"
    assert "budget_exceeded" in {
        item["subtype"] for item in _reviews(env.app, claim_id, "EXCEPTION")
    }
    calls = _events(env.app, "model.called", claim_id)
    assert any(_payload(event)["detail"]["cost_usd"] == 0.19 for event in calls)


def test_input_token_ceiling_is_enforced_before_the_model_call(tmp_path):
    env = _build(tmp_path, "budget-token", model=_model())
    seeded = _seed(env, "BT")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    env.service.config.commentary["max_input_tokens"] = 1

    _generate(env, seeded)
    assert [
        call for call in env.model.calls
        if call["inputs"].get("task") == "pack_note_commentary"
    ] == []
    assert "budget_exceeded" in {
        item["subtype"] for item in _reviews(env.app, claim_id, "EXCEPTION")
    }


# --- §1.9 exactly one open review -----------------------------------------------------


def test_regeneration_leaves_exactly_one_open_note_review(tmp_path):
    env = _build(
        tmp_path,
        "single-review",
        model=_model(
            commentary=[_commentary(), _commentary()],
            gnote=[_g_note_clean(), _g_note_clean()],
        ),
    )
    seeded = _seed(env, "S")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    assert _generate(env, seeded, key="one").status_code == 201
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h())
    seeded["readiness"] = card.json()
    second = _generate(env, seeded, key="two")
    assert second.status_code == 201

    reviews = _reviews(env.app, claim_id, "NOTE_REVIEW")
    assert len(reviews) == 2
    assert [item["status"] for item in reviews] == ["cancelled", "open"]

    drafts = _rows(
        env.app,
        "SELECT id, version, status FROM note_drafts WHERE claim_id = :c ORDER BY version",
        c=claim_id,
    )
    assert [(row["version"], row["status"]) for row in drafts] == [
        (1, "superseded"), (2, "in_review"),
    ]
    open_review = reviews[1]
    assert _payload(open_review)["note_draft_id"] == drafts[1]["id"]
    assert second.json()["note_review_item_id"] == open_review["id"]


def test_late_older_note_action_cannot_supersede_the_newer_pack_note(tmp_path):
    from agent_runtime import Action

    env = _build(
        tmp_path,
        "note-order",
        model=_model(
            commentary=[_commentary(), _commentary()],
            gnote=[_g_note_clean(), _g_note_clean()],
        ),
    )
    seeded = _seed(env, "NO")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    # Leave note drafting at L1 so both captured actions can be delivered in
    # deliberately reversed order.
    assert _generate(env, seeded, key="one").status_code == 201
    seeded["readiness"] = env.client.get(
        f"/claims/{claim_id}/approval-pack/readiness", headers=_h()
    ).json()
    assert _generate(env, seeded, key="two").status_code == 201

    staged = [
        _payload(item)["action"]
        for item in _reviews(env.app, claim_id, "DRAFT_RELEASE")
        if _payload(item).get("capability_id") == "pack.note_draft"
    ]
    assert len(staged) == 2
    staged.sort(key=lambda action: action["payload"]["merged_event_id"])
    # Resolve event order from the immutable merged index, then execute newest first.
    merged_order = {
        event["id"]: _payload(event)["version"]
        for event in _events(env.app, "pack.merged", claim_id)
    }
    staged.sort(
        key=lambda action: merged_order[action["payload"]["merged_event_id"]],
        reverse=True,
    )
    for captured in staged:
        env.service.execute_note_draft(
            Action(type=captured["type"], payload=dict(captured["payload"]))
        )

    drafts = _rows(
        env.app,
        "SELECT version, status, body FROM note_drafts WHERE claim_id = :c ORDER BY version",
        c=claim_id,
    )
    decoded = [
        {
            **row,
            "body": json.loads(row["body"]) if isinstance(row["body"], str) else row["body"],
        }
        for row in drafts
    ]
    assert [row["body"]["merged_pack"]["version"] for row in decoded] == [2, 1]
    assert [row["status"] for row in decoded] == ["in_review", "superseded"]
    env.app.state.review_queue.backfill("agent:approval_pack")
    assert [item["status"] for item in _reviews(env.app, claim_id, "NOTE_REVIEW")] == [
        "open"
    ]


def test_cancelled_review_is_ledgered_and_cannot_be_cancelled_twice(tmp_path):
    env = _build(
        tmp_path,
        "cancel",
        model=_model(
            commentary=[_commentary(), _commentary()],
            gnote=[_g_note_clean(), _g_note_clean()],
        ),
    )
    seeded = _seed(env, "C")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    _generate(env, seeded, key="one")
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h())
    seeded["readiness"] = card.json()
    _generate(env, seeded, key="two")

    for _ in range(64):
        if env.app.state.dispatcher.dispatch_once() == 0:
            break
    ledger = _rows(
        env.app,
        "SELECT action FROM audit_ledger WHERE claim_id = :c AND action = 'review.cancelled'",
        c=claim_id,
    )
    assert len(ledger) == 1

    cancelled = _reviews(env.app, claim_id, "NOTE_REVIEW")[0]
    from claim_core import ClaimCoreError

    with pytest.raises(ClaimCoreError) as error:
        env.app.state.review_queue.cancel(
            cancelled["id"], actor="agent:approval_pack", reason="repeat"
        )
    assert error.value.code == "ALREADY_RESOLVED"


def test_only_originating_producer_may_cancel_an_approval_note(tmp_path):
    env = _build(tmp_path, "cancel-owner", model=_model())
    seeded = _seed(env, "CO")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    _generate(env, seeded)
    review = _reviews(env.app, claim_id, "NOTE_REVIEW")[0]

    from claim_core import ClaimCoreError

    with pytest.raises(ClaimCoreError) as error:
        env.app.state.review_queue.cancel(
            review["id"], actor="agent:not-the-producer", reason="unauthorised"
        )
    assert error.value.code == "FORBIDDEN_ROLE"
    assert _reviews(env.app, claim_id, "NOTE_REVIEW")[0]["status"] == "open"


def test_draft_release_cannot_use_the_note_supersession_cancel_path(tmp_path):
    env = _build(tmp_path, "cancel-type", model=_model())
    seeded = _seed(env, "CT")
    response = _generate(env, seeded)
    assert response.status_code == 202

    from claim_core import ClaimCoreError

    with pytest.raises(ClaimCoreError) as error:
        env.app.state.review_queue.cancel(
            response.json()["review_item_id"],
            actor="agent:approval_pack",
            reason="not a note supersession",
        )
    assert error.value.code == "CANCELLATION_NOT_ALLOWED"


def test_review_projection_replays_cancellation_from_the_event_spine(tmp_path):
    env = _build(
        tmp_path,
        "cancel-rebuild",
        model=_model(
            commentary=[_commentary(), _commentary()],
            gnote=[_g_note_clean(), _g_note_clean()],
        ),
    )
    seeded = _seed(env, "CB")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    _generate(env, seeded, key="one")
    seeded["readiness"] = env.client.get(
        f"/claims/{claim_id}/approval-pack/readiness", headers=_h()
    ).json()
    _generate(env, seeded, key="two")
    assert [item["status"] for item in _reviews(env.app, claim_id, "NOTE_REVIEW")] == [
        "cancelled", "open",
    ]

    with env.app.state.engine.begin() as connection:
        connection.execute(text("DELETE FROM review_items"))
    env.app.state.review_queue.backfill("agent:approval_pack")
    assert [item["status"] for item in _reviews(env.app, claim_id, "NOTE_REVIEW")] == [
        "cancelled", "open",
    ]


# --- §1.4 / #227 render timestamp -----------------------------------------------------


def test_renderer_receives_the_binding_eat_timestamp_header(tmp_path):
    env = _build(tmp_path, "stamp", model=_model(commentary=[], gnote=[]))
    seeded = _seed(env, "T")
    _set_level(env, "pack.merge", "L3")
    assert _generate(env, seeded).status_code == 201

    assert env.renderer.calls
    for html in env.renderer.calls:
        assert "Rendered 2026-07-22 11:00 EAT" in html
        assert html.index("Rendered 2026-07-22 11:00 EAT") < html.index("<html>")


def test_a_later_render_time_does_not_reuse_a_stale_timestamp_header(tmp_path):
    env = _build(tmp_path, "stamp-cache", model=_model(commentary=[], gnote=[]))
    seeded = _seed(env, "TC")
    claim_id = seeded["claim_id"]
    _set_level(env, "pack.merge", "L3")
    assert _generate(env, seeded, key="one").status_code == 201
    first_calls = len(env.renderer.calls)

    env.clock.advance(days=1)
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h())
    seeded["readiness"] = card.json()
    assert _generate(env, seeded, key="two").status_code == 201

    assert len(env.renderer.calls) == first_calls * 2
    assert "Rendered 2026-07-23 11:00 EAT" in env.renderer.calls[-1]


# --- ED-7 generated OpenAPI artifact ---------------------------------------------------


def test_committed_openapi_snapshot_is_current_and_adds_no_sign_or_route_surface():
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [sys.executable, "tools/openapi_snapshot.py", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    document = json.loads((REPO / "docs" / "openapi" / "approval_pack.json").read_text())
    prefix = "/claims/{claim_id}/approval-pack"
    review = "/reviews/{review_id}/approval-note"
    assert sorted(document["paths"]) == [
        f"{prefix}/artifacts/{{event_id}}",
        f"{prefix}/generate",
        f"{prefix}/manifest/{{item_id}}/sources",
        f"{prefix}/manifest/{{item_id}}/upload",
        f"{prefix}/note-drafts",
        f"{prefix}/readiness",
        f"{prefix}/versions",
        review,
        f"{review}/draft",
    ]
    assert sorted(document["paths"][f"{prefix}/readiness"]) == ["get"]
    assert sorted(document["paths"][f"{prefix}/generate"]) == ["post"]
    # PACKET-19 signs and routes through the closed PRD-04 resolution endpoint.
    # No signing, routing, or approval verb ever becomes its own HTTP surface.
    for path in document["paths"]:
        assert not any(word in path for word in ("sign", "route", "approve", "reject"))


# --- pack validation refuses rather than defaulting ------------------------------------


def _write_pack(tmp_path: pathlib.Path) -> pathlib.Path:
    import shutil

    pack = tmp_path / "motor"
    shutil.copytree(MOTOR_PACK, pack)
    return pack


def _load(pack: pathlib.Path):
    from approval_pack_agent.config import load_config

    return load_config(pack)


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ("duplicate_order", "invalid values"),
        ("non_contiguous_order", "contiguous"),
        ("unknown_key", "invalid keys"),
        ("unknown_doc_type", "unregistered doc types"),
        ("unknown_conversion", "invalid values"),
        ("projection_selector_on_wrong_item", "projection_or_upload"),
    ],
)
def test_manifest_validation_refuses_a_malformed_pack(tmp_path, mutation, fragment):
    import yaml

    from approval_pack_agent.config import PackConfigError

    pack = _write_pack(tmp_path)
    path = pack / "approval_pack" / "manifest.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if mutation == "duplicate_order":
        payload["items"][1]["order"] = 5
    elif mutation == "non_contiguous_order":
        payload["items"][1]["order"] = 99
    elif mutation == "unknown_key":
        payload["items"][0]["retention"] = "forever"
    elif mutation == "unknown_doc_type":
        payload["items"][2]["doc_types"] = ["policy_schedule"]
    elif mutation == "unknown_conversion":
        payload["items"][0]["conversion"] = "ocr"
    else:
        payload["items"][0]["selector"] = "projection_or_upload"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError) as error:
        _load(pack)
    assert fragment in str(error.value)


def test_note_slot_map_refuses_an_invented_value_or_placeholder(tmp_path):
    import yaml

    from approval_pack_agent.config import PackConfigError

    pack = _write_pack(tmp_path)
    path = pack / "approval_pack" / "note.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["slots"]["repair_amount"]["value"] = 0
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError) as error:
        _load(pack)
    assert "must not carry a value" in str(error.value)

    payload["slots"]["repair_amount"].pop("value")
    payload["slots"]["amount_payable"]["placeholder"] = "TBC"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError) as error:
        _load(pack)
    assert "mandated placeholder" in str(error.value)


def test_render_policy_must_stay_offline(tmp_path):
    import yaml

    from approval_pack_agent.config import PackConfigError

    pack = _write_pack(tmp_path)
    path = pack / "approval_pack" / "render.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["html"]["network_enabled"] = True
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError) as error:
        _load(pack)
    assert "offline contract" in str(error.value)


def test_commentary_sections_must_match_the_note_commentary_slots(tmp_path):
    import yaml

    from approval_pack_agent.config import PackConfigError

    pack = _write_pack(tmp_path)
    path = pack / "approval_pack" / "commentary.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["sections"] = ["incident_summary"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError):
        _load(pack)


# --- resolver never guesses ------------------------------------------------------------


def _item_state(env: Env, claim_id: str, item_id: str) -> dict[str, Any]:
    card = env.client.get(f"/claims/{claim_id}/approval-pack/readiness", headers=_h()).json()
    return next(item for item in card["items"] if item["id"] == item_id)


def test_rejected_missing_and_corrupt_sources_each_block_their_own_item(tmp_path):
    env = _build(tmp_path, "resolver", model=_model())
    seeded = _seed(env, "X")
    claim_id = seeded["claim_id"]

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE documents SET status = 'rejected' WHERE id = :id"),
            {"id": seeded["documents"]["logbook"]},
        )
    logbook = _item_state(env, claim_id, "logbook")
    assert logbook["state"] == "missing"
    assert _item_state(env, claim_id, "claim_form")["state"] == "ready"

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE documents SET status = 'verified', sha256 = 'deadbeef' WHERE id = :id"),
            {"id": seeded["documents"]["logbook"]},
        )
    assert _item_state(env, claim_id, "logbook")["state"] == "invalid"
    assert {
        blocker["code"] for blocker in _item_state(env, claim_id, "logbook")["blockers"]
    } == {"digest_mismatch"}


def test_a_non_pdf_passthrough_source_is_never_silently_imaged(tmp_path):
    env = _build(tmp_path, "resolver-mime", model=_model())
    seeded = _seed(env, "M")
    claim_id = seeded["claim_id"]

    row = env.app.state.claim_service.add_document(
        claim_id,
        filename="policy.png",
        mime="image/png",
        content=_png("blue"),
        source_channel="test_fixture",
        source_ref="src-policy-png",
        actor=OFFICER,
    )
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE documents SET status = 'verified' WHERE id = :id"), {"id": row.id}
        )
    response = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "document", "id": row.id}]},
        headers=_h(),
    )
    assert response.status_code == 200
    item = _item_state(env, claim_id, "policy_document")
    assert item["state"] == "invalid"
    assert {blocker["code"] for blocker in item["blockers"]} == {"conversion_unsupported"}


def test_projection_backed_items_report_pending_integration_before_an_upload(tmp_path):
    env = _build(tmp_path, "resolver-projection", model=_model())
    response = env.client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.0.0"}, headers=_h()
    )
    claim_id = response.json()["id"]
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET status = 'RESERVED' WHERE id = :claim_id"),
            {"claim_id": claim_id},
        )
    for item_id in ("assessor_payment_request", "claim_details_report"):
        item = _item_state(env, claim_id, item_id)
        assert item["state"] == "pending_integration"
        assert item["waivable"] is False
        assert item["required"] is True


def test_selection_cardinality_and_kind_are_validated_before_anything_is_recorded(tmp_path):
    env = _build(tmp_path, "resolver-cardinality", model=_model())
    seeded = _seed(env, "K")
    claim_id = seeded["claim_id"]
    documents = seeded["documents"]

    duplicated = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={
            "sources": [
                {"kind": "document", "id": documents["policy"]},
                {"kind": "document", "id": documents["claim_form"]},
            ]
        },
        headers=_h(),
    )
    assert duplicated.status_code == 422
    assert duplicated.json()["code"] == "SOURCE_CARDINALITY_INVALID"

    wrong_kind = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "communication", "id": documents["policy"]}]},
        headers=_h(),
    )
    assert wrong_kind.status_code == 422

    upload_item = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/claim_details_report/sources",
        json={"sources": [{"kind": "document", "id": documents["policy"]}]},
        headers=_h(),
    )
    assert upload_item.status_code == 422
    assert upload_item.json()["code"] == "SOURCE_KIND_NOT_ALLOWED"

    unknown = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/not_an_item/sources",
        json={"sources": [{"kind": "document", "id": documents["policy"]}]},
        headers=_h(),
    )
    assert unknown.status_code == 404


def test_upload_rejects_a_non_pdf_and_an_unlisted_item(tmp_path):
    env = _build(tmp_path, "upload-guard", model=_model())
    seeded = _seed(env, "U")
    claim_id = seeded["claim_id"]

    not_pdf = env.client.post(
        f"/claims/{claim_id}/approval-pack/manifest/claim_details_report/upload",
        files={"file": ("x.png", _png("red"), "image/png")},
        headers=_h(),
    )
    assert not_pdf.status_code == 422
    assert not_pdf.json()["code"] == "INVALID_PDF"

    wrong_item = env.client.post(
        f"/claims/{claim_id}/approval-pack/manifest/policy_document/upload",
        files={"file": ("x.pdf", _pdf("X"), "application/pdf")},
        headers=_h(),
    )
    assert wrong_item.status_code == 404
