"""PACKET-18 acceptance — PRD-08 approval-pack backend.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-18_approval_pack_backend.md §3. No live browser, S3, Graph,
ICON, network call, or provider model is permitted. HTML rendering and the two
MODEL_HEAVY tasks use strict injected fakes; time is fixed. This packet ends at
PACK_READY with an unresolved NOTE_REVIEW. Editing, autosave, signing, routing,
T-03 and paste-assist belong to PACKET-19.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import yaml
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

OFFICER = "user:01HPACKOFFICER0000000AAAA"
MANAGER = "user:01HPACKMANAGER0000000AAAA"
OUTSIDER = "user:01HPACKOUTSIDER000000AAAA"
T0 = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)  # 11:00 EAT

ESTIMATE = 26_100_000
ASSESSED = 13_627_600
PAV = 150_000_000
SUM_INSURED = 60_000_000
EXCESS = 1_500_000
SAVING = 12_472_400

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

    def advance(self, *, seconds: int = 0) -> None:
        self.now += timedelta(seconds=seconds)


def _pdf(*pages: str) -> bytes:
    import fitz

    document = fitz.open()
    for value in pages:
        page = document.new_page()
        page.insert_text((72, 72), value)
    result = document.tobytes(garbage=4, deflate=True)
    document.close()
    return result


def _png(colour: str) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (640, 480), colour).save(output, format="PNG")
    return output.getvalue()


@dataclass
class RenderCall:
    html: str
    policy: Any


class FakeHtmlRenderer:
    """Duck-typed HtmlPdfRenderer. It never reads the network."""

    def __init__(self) -> None:
        self.calls: list[RenderCall] = []
        self.fail_at: set[int] = set()

    @staticmethod
    def _policy_value(policy: Any, name: str) -> Any:
        return policy[name] if isinstance(policy, dict) else getattr(policy, name)

    def render(self, html: str, *, policy: Any) -> Any:
        self.calls.append(RenderCall(html=html, policy=policy))
        assert self._policy_value(policy, "network_enabled") is False
        assert set(self._policy_value(policy, "allowed_schemes")) == {"data", "cid"}
        assert self._policy_value(policy, "timeout_seconds") == 30
        assert self._policy_value(policy, "margin_mm") == 18
        if len(self.calls) in self.fail_at:
            raise TimeoutError("synthetic chromium timeout")
        # A remote URL remains inert text. The fake deliberately has no HTTP client.
        return SimpleNamespace(
            pdf_bytes=_pdf(f"HTML source {len(self.calls)}", html[:100]),
            fallback_used=False,
            fallback_reason=None,
            blocked_resource_count=html.count("https://") + html.count("http://"),
            chromium_version="fake-pinned-chromium",
        )


class TaskModel:
    """Strict fake for commentary and G-NOTE; task names must be pack-driven."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[dict[str, Any]] = []

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        task = inputs.get("task")
        self.calls.append({"tier": tier, "schema": schema, "inputs": dict(inputs)})
        queue = self.responses.get(str(task), [])
        if not queue:
            raise AssertionError(f"unexpected/unscripted model task: {task!r}")
        return {
            "data": queue.pop(0),
            "cost_usd": 0.01,
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
                "content": (
                    "The assessed amount is KES 136,276 and the applicable "
                    "excess is KES 15,000."
                ),
                "numbers_used": ["136,276", "15,000"],
            },
            {
                "template_slot": "savings_narrative",
                "content": (
                    "The estimate was KES 261,000 and the assessed amount is "
                    "KES 136,276, giving a recorded saving of KES 124,724."
                ),
                "numbers_used": ["261,000", "136,276", "124,724"],
            },
        ]
    }


def _numeric_claim(
    text_value: str,
    *,
    source_kind: str,
    source_ref: str,
    observed_value: int | str,
    value_type: str = "money",
) -> dict[str, Any]:
    return {
        "text": text_value,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "observed_value": observed_value,
        "value_type": value_type,
    }


def _g_note_clean() -> dict[str, Any]:
    return {
        "numeric_claims": [
            _numeric_claim(
                "136,276", source_kind="claim_field",
                source_ref="assessment.agreed_quote", observed_value=ASSESSED,
            ),
            _numeric_claim(
                "15,000", source_kind="claim_field",
                source_ref="policy.excess_amount", observed_value=EXCESS,
            ),
            _numeric_claim(
                "261,000", source_kind="claim_field",
                source_ref="assessment.estimate_total", observed_value=ESTIMATE,
            ),
            _numeric_claim(
                "136,276", source_kind="claim_field",
                source_ref="assessment.agreed_quote", observed_value=ASSESSED,
            ),
            _numeric_claim(
                "124,724", source_kind="savings_ledger",
                source_ref="SAVE-HEADER", observed_value=SAVING,
            ),
        ],
        "unsupported_assertions": [],
        "missing_sections": [],
        "tone_ok": True,
    }


def _g_note_injected() -> dict[str, Any]:
    result = _g_note_clean()
    result["numeric_claims"][-1] = _numeric_claim(
        "999,999",
        source_kind="claim_field",
        source_ref="assessment.agreed_quote",
        observed_value=99_999_900,
    )
    return result


def _model(*, commentary: list[dict[str, Any]] | None = None,
           gnote: list[dict[str, Any]] | None = None) -> TaskModel:
    return TaskModel(
        {
            "pack_note_commentary": commentary or [_commentary()],
            "g_note_grade": gnote or [_g_note_clean()],
        }
    )


def _h(actor: str = OFFICER) -> dict[str, str]:
    return {"X-Actor": actor}


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _rows(app: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    with app.state.engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings()
        return [
            {key: _decode(value) for key, value in dict(row).items()}
            for row in rows
        ]


def _events(app: Any, type_: str, claim_id: str | None = None) -> list[dict[str, Any]]:
    rows = _rows(
        app,
        "SELECT id, claim_id, type, payload, actor FROM events "
        "WHERE type = :type ORDER BY seq",
        type=type_,
    )
    return rows if claim_id is None else [row for row in rows if row["claim_id"] == claim_id]


def _drafts(app: Any, claim_id: str) -> list[dict[str, Any]]:
    return _rows(
        app,
        "SELECT id, claim_id, version, body, status, edited_by, signed_by, signed_at "
        "FROM note_drafts WHERE claim_id = :claim_id ORDER BY version",
        claim_id=claim_id,
    )


def _reviews(app: Any, claim_id: str, type_: str) -> list[dict[str, Any]]:
    return _rows(
        app,
        "SELECT id, type, subtype, status, payload FROM review_items "
        "WHERE claim_id = :claim_id AND type = :type ORDER BY created_at, id",
        claim_id=claim_id,
        type=type_,
    )


def _drain(app: Any, cycles: int = 128) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


class Env:
    def __init__(self, app: Any, client: Any, clock: FixedClock,
                 renderer: FakeHtmlRenderer, model: TaskModel) -> None:
        self.app = app
        self.client = client
        self.clock = clock
        self.renderer = renderer
        self.model = model


def _build(tmp_path: pathlib.Path, name: str, *, model: TaskModel,
           renderer: FakeHtmlRenderer | None = None) -> Env:
    # I001 suppressed: package becomes first-party only after implementation.
    from fastapi.testclient import TestClient  # noqa: I001

    from agent_runtime import build_agent_runtime
    from approval_pack_agent import build_approval_pack_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    clock = FixedClock()
    renderer = renderer or FakeHtmlRenderer()
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(database_url, clock=clock)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app, model_client=model)
    build_review_queue(
        app,
        roles={
            OFFICER: "claims_officer",
            MANAGER: "claims_manager",
            OUTSIDER: "auditor",
        },
    )
    build_agent_runtime(app)
    build_approval_pack_agent(app, model_client=model, html_renderer=renderer)
    return Env(app, TestClient(app), clock, renderer, model)


def _create_reserved(env: Env, suffix: str = "A") -> str:
    response = env.client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(),
    )
    assert response.status_code == 201, response.text
    claim_id = response.json()["id"]
    with env.app.state.engine.begin() as connection:
        # Upstream PRD-07 is not under test here. This owner fixture starts at the
        # PRD-08 trigger and leaves a visible synthetic status-change event below.
        connection.execute(
            text("UPDATE claims SET status = 'RESERVED' WHERE id = :claim_id"),
            {"claim_id": claim_id},
        )
        connection.execute(
            text(
                "INSERT INTO chase_checklists "
                "(id, claim_id, purpose, status, blocking, requester_party_id, created_at) "
                "VALUES (:id, :claim_id, 'claim_docs', 'complete', true, NULL, :created_at)"
            ),
            {
                "id": f"CHECKLIST-{suffix}",
                "claim_id": claim_id,
                "created_at": T0,
            },
        )
    with Session(env.app.state.engine) as session:
        env.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="claim.status_changed",
            payload={"from": "REGISTERED", "to": "RESERVED", "fixture": True},
            actor="system:test",
            correlation_id=None,
        )
        session.commit()
    return claim_id


def _write_active_fields(env: Env, claim_id: str) -> None:
    writes = [
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
    response = env.client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": writes},
        headers=_h(),
    )
    assert response.status_code == 200, response.text


def _add_document(
    env: Env,
    claim_id: str,
    *,
    filename: str,
    content: bytes,
    doc_type: str | None,
    mime: str = "application/pdf",
    status: str = "verified",
) -> str:
    row = env.app.state.claim_service.add_document(
        claim_id,
        filename=filename,
        mime=mime,
        content=content,
        source_channel="test_fixture",
        source_ref=f"src-{filename}",
        actor=OFFICER,
    )
    page_count = None
    if mime == "application/pdf":
        import fitz

        document = fitz.open(stream=content, filetype="pdf")
        page_count = document.page_count
        document.close()
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE documents SET doc_type = :doc_type, status = :status, "
                "page_count = :page_count WHERE id = :id"
            ),
            {
                "id": row.id,
                "doc_type": doc_type,
                "status": status,
                "page_count": page_count,
            },
        )
    return row.id


def _add_communication(env: Env, claim_id: str, purpose: str, sequence: int) -> str:
    body = (
        f"<html><body><h1>{purpose}</h1>"
        "<img src='https://tracking.invalid/pixel.png'>"
        "<p>Archived claim correspondence.</p></body></html>"
    )
    row, created = env.app.state.claim_service.record_inbound_communication(
        graph_message_id=f"msg-pack-{claim_id[-5:]}-{sequence}",
        claim_id=claim_id,
        thread_id=f"thread-{claim_id}",
        from_addr="broker@example.co.ke",
        to_addrs=["claims@mayfair.co.ke"],
        subject=purpose,
        body_text=body,
    )
    assert created
    return row.id


def _select(env: Env, claim_id: str, item_id: str,
            sources: list[dict[str, str]], *, expected: int = 200) -> Any:
    response = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/{item_id}/sources",
        json={"sources": sources},
        headers=_h(),
    )
    assert response.status_code == expected, response.text
    return response


def _upload(env: Env, claim_id: str, item_id: str, filename: str) -> Any:
    response = env.client.post(
        f"/claims/{claim_id}/approval-pack/manifest/{item_id}/upload",
        files={"file": (filename, _pdf(filename), "application/pdf")},
        headers=_h(),
    )
    assert response.status_code == 201, response.text
    return response.json()


@dataclass
class Reference:
    claim_id: str
    document_ids: dict[str, str]
    communication_ids: dict[str, str]
    readiness: dict[str, Any]


def _seed_reference(env: Env, *, suffix: str = "A", second_claim_form: bool = False,
                    select_claim_form: bool = False) -> Reference:
    claim_id = _create_reserved(env, suffix)
    _write_active_fields(env, claim_id)

    documents = {
        "policy": _add_document(
            env, claim_id, filename="policy.pdf",
            content=_pdf("POLICY", "SCHEDULE"), doc_type=None,
        ),
        "claim_form": _add_document(
            env, claim_id, filename="claim-form.pdf",
            content=_pdf("CLAIM FORM"), doc_type="claim_form",
        ),
        "logbook": _add_document(
            env, claim_id, filename="logbook.pdf",
            content=_pdf("LOGBOOK"), doc_type="logbook",
        ),
        "driving_licence": _add_document(
            env, claim_id, filename="licence.pdf",
            content=_pdf("DRIVING LICENCE"), doc_type="driving_licence",
        ),
        "kra_pin_cert": _add_document(
            env, claim_id, filename="kra-pin.pdf",
            content=_pdf("KRA PIN"), doc_type="kra_pin_cert",
        ),
        "photo_1": _add_document(
            env, claim_id, filename="damage-front.png", content=_png("red"),
            doc_type="photo_damage", mime="image/png",
        ),
        "photo_2": _add_document(
            env, claim_id, filename="damage-rear.png", content=_png("green"),
            doc_type="photo_damage", mime="image/png",
        ),
        "photo_3": _add_document(
            env, claim_id, filename="damage-side.png", content=_png("blue"),
            doc_type="photo_damage", mime="image/png",
        ),
        "estimate": _add_document(
            env, claim_id, filename="estimate.pdf",
            content=_pdf("REPAIR ESTIMATE"), doc_type="repair_estimate",
        ),
        "report": _add_document(
            env, claim_id, filename="assessor-report.pdf",
            content=_pdf("ASSESSOR REPORT", "ASSESSOR APPENDIX"),
            doc_type="assessor_report",
        ),
        "supplier_quote": _add_document(
            env, claim_id, filename="supplier-breakdown.pdf",
            content=_pdf("SUPPLIER BREAKDOWN"), doc_type=None,
        ),
    }
    if second_claim_form:
        documents["claim_form_2"] = _add_document(
            env, claim_id, filename="claim-form-revised.pdf",
            content=_pdf("SECOND CLAIM FORM"), doc_type="claim_form",
        )

    communications = {
        "intimation": _add_communication(env, claim_id, "Intimation email", 1),
        "engagement": _add_communication(env, claim_id, "Assessor engagement", 2),
        "supplier": _add_communication(env, claim_id, "Supplier quote email", 3),
    }

    with Session(env.app.state.engine) as session:
        env.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="assessment.report_received",
            payload={
                "claim_id": claim_id,
                "document_id": documents["report"],
                "assessor_party_id": "PARTY-ASSESSOR",
                "vendor_id": "V-ASSESSOR",
            },
            actor="agent:assessment",
            correlation_id=None,
        )
        session.commit()

    _select(env, claim_id, "policy_document", [
        {"kind": "document", "id": documents["policy"]},
    ])
    _select(env, claim_id, "intimation_email", [
        {"kind": "communication", "id": communications["intimation"]},
    ])
    _select(env, claim_id, "assessor_engagement_email", [
        {"kind": "communication", "id": communications["engagement"]},
    ])
    _select(env, claim_id, "supplier_quotes", [
        {"kind": "communication", "id": communications["supplier"]},
        {"kind": "document", "id": documents["supplier_quote"]},
    ])
    if select_claim_form:
        _select(env, claim_id, "claim_form", [
            {"kind": "document", "id": documents["claim_form"]},
        ])
    _upload(env, claim_id, "assessor_payment_request", "assessor-payment.pdf")
    _upload(env, claim_id, "claim_details_report", "claim-details.pdf")

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO savings_ledger "
                "(id, claim_id, kind, baseline_amount, achieved_amount, evidence, "
                "vendor_id, occurred_at) VALUES "
                "('SAVE-HEADER', :claim_id, 'assessment_negotiation', :baseline, "
                ":achieved, :evidence, NULL, :occurred_at)"
            ),
            {
                "claim_id": claim_id,
                "baseline": ESTIMATE,
                "achieved": ASSESSED,
                "evidence": json.dumps({
                    "calc_run_id": "CALC-C05",
                    "citations": [{"document_id": documents["report"], "page": 1}],
                }),
                "occurred_at": T0,
            },
        )

    response = env.client.get(
        f"/claims/{claim_id}/approval-pack/readiness", headers=_h()
    )
    assert response.status_code == 200, response.text
    return Reference(claim_id, documents, communications, response.json())


def _set_level(env: Env, capability_id: str, level: str) -> None:
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
            {"level": level, "id": capability_id},
        )


def _generate(env: Env, reference: Reference, key: str = "generate-1") -> Any:
    return env.client.post(
        f"/claims/{reference.claim_id}/approval-pack/generate",
        json={"readiness_fingerprint": reference.readiness["fingerprint"]},
        headers={**_h(), "Idempotency-Key": key},
    )


# --- Pack/config pins ---------------------------------------------------------------


def test_pack_contract_pins_exact_manifest_note_and_capability_levels():
    manifest = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["version"] == 1
    items = manifest["items"]
    assert [item["order"] for item in items] == list(range(1, 14))
    assert [item["id"] for item in items] == [
        "policy_document",
        "intimation_email",
        "claim_form",
        "logbook",
        "driving_licence",
        "kra_pin_cert",
        "photos",
        "repair_estimate",
        "assessor_engagement_email",
        "assessor_report",
        "supplier_quotes",
        "assessor_payment_request",
        "claim_details_report",
    ]
    assert all(item["required"] is True and item["waivable"] is False for item in items)
    assert items[6]["conversion"] == "photos_2up" and items[6]["repeatable"] is True
    assert items[11]["source_kinds"] == ["projection_readback", "upload"]
    assert items[12]["source_kinds"] == ["projection_readback", "upload"]

    note = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "note.yaml").read_text(encoding="utf-8")
    )
    assert note["computed_slots"] == [
        "amount_payable", "repair_amount", "assessed_amount", "estimate", "excess",
        "pav", "percent_si", "percent_pav", "garage", "loss_location",
        "third_party_count", "excess_protector", "duty_paid",
        "recovery_register_flag", "subrogation",
    ]
    assert note["commentary_slots"] == [
        "incident_summary", "excess_vs_max", "savings_narrative",
    ]
    assert note["slots"]["amount_payable"]["placeholder"] == "PENDING CAPTURE"
    for slot in (
        "repair_amount", "percent_si", "percent_pav", "garage",
        "third_party_count", "duty_paid", "recovery_register_flag", "subrogation",
    ):
        assert note["slots"][slot]["status"] == "blocked_on_inputs"
        assert "value" not in note["slots"][slot]

    registry = yaml.safe_load(
        (MOTOR_PACK / "templates" / "registry.yaml").read_text(encoding="utf-8")
    )
    t01 = next(item for item in registry["templates"] if item["id"] == "T-01")
    assert t01["status"] == "live"
    assert t01["body_ref"] == "templates/T-01.j2"
    assert t01["min_verification"] == "human_verified"
    assert t01["required_fields"] == list(ACTIVE_FIELDS)

    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8")
    )
    by_id = {row["id"]: row for row in policies["capabilities"]}
    assert by_id["pack.merge"]["initial_level"] == "L1"
    assert by_id["pack.merge"]["max_level"] == "L4"
    assert by_id["pack.note_draft"]["initial_level"] == "L1"
    assert by_id["pack.note_draft"]["max_level"] == "L3"
    assert by_id["pack.route"]["initial_level"] == "L1"

    commentary = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "commentary.yaml").read_text(encoding="utf-8")
    )
    assert commentary["task"] == "pack_note_commentary"
    assert commentary["tier"] == "MODEL_HEAVY"
    assert commentary["prompt_ref"] == "pack.note_commentary@v1"
    assert commentary["incident_summary_max_words"] == 80

    harness = yaml.safe_load(
        (MOTOR_PACK / "eval" / "harness.yaml").read_text(encoding="utf-8")
    )
    assert harness["model_graders"]["G-NOTE"]["rubric_ref"] == "T-01@1"
    assert harness["model_graders"]["G-NOTE"]["required_section_ids"] == [
        "incident_summary", "excess_vs_max", "savings_narrative",
    ]


# --- Schema + readiness fail-closed -------------------------------------------------


def test_note_drafts_schema_is_binding_prd_08_ddl(tmp_path):
    env = _build(tmp_path, "schema", model=_model())
    inspector = inspect(env.app.state.engine)
    columns = {column["name"]: column for column in inspector.get_columns("note_drafts")}
    assert list(columns) == [
        "id", "claim_id", "version", "body", "status",
        "edited_by", "signed_by", "signed_at",
    ]
    assert columns["id"]["nullable"] is False
    assert columns["claim_id"]["nullable"] is False
    assert columns["version"]["nullable"] is False
    assert columns["body"]["nullable"] is False
    assert columns["status"]["nullable"] is False
    assert columns["edited_by"]["nullable"] is True
    assert columns["signed_by"]["nullable"] is True
    assert columns["signed_at"]["nullable"] is True
    unique_sets = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("note_drafts")
    }
    assert ("claim_id", "version") in unique_sets
    checks = " ".join(
        str(item.get("sqltext", "")) for item in inspector.get_check_constraints("note_drafts")
    )
    for status in ("draft", "in_review", "signed", "superseded"):
        assert status in checks


def test_readiness_never_guesses_ambiguity_and_selection_is_claim_isolated(tmp_path):
    env = _build(tmp_path, "readiness", model=_model())
    reference = _seed_reference(env, suffix="R", second_claim_form=True)

    card = reference.readiness
    assert card["ready"] is False
    assert len(card["items"]) == 13
    assert [item["order"] for item in card["items"]] == list(range(1, 14))
    claim_form = next(item for item in card["items"] if item["id"] == "claim_form")
    assert claim_form["state"] == "ambiguous"
    assert set(source["id"] for source in claim_form["sources"]) == {
        reference.document_ids["claim_form"], reference.document_ids["claim_form_2"],
    }
    assert _events(env.app, "pack.merged", reference.claim_id) == []

    # A source from another claim is deliberately indistinguishable from absent.
    other = _create_reserved(env, "OTHER")
    other_doc = _add_document(
        env, other, filename="other-policy.pdf", content=_pdf("OTHER"), doc_type=None,
    )
    response = _select(
        env,
        reference.claim_id,
        "policy_document",
        [{"kind": "document", "id": other_doc}],
        expected=404,
    )
    assert response.json()["code"] in {"SOURCE_NOT_FOUND", "CLAIM_NOT_FOUND"}

    # Explicit choice resolves only this item. Same selection is append-idempotent.
    before = len(_events(env.app, "pack.sources_selected", reference.claim_id))
    _select(env, reference.claim_id, "claim_form", [
        {"kind": "document", "id": reference.document_ids["claim_form"]},
    ])
    after = len(_events(env.app, "pack.sources_selected", reference.claim_id))
    assert after == before + 1
    _select(env, reference.claim_id, "claim_form", [
        {"kind": "document", "id": reference.document_ids["claim_form"]},
    ])
    assert len(_events(env.app, "pack.sources_selected", reference.claim_id)) == after

    ready = env.client.get(
        f"/claims/{reference.claim_id}/approval-pack/readiness", headers=_h()
    )
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["fingerprint"] != card["fingerprint"]

    # Auditor/portal-like role cannot select sources; no event is appended.
    denied = env.client.put(
        f"/claims/{reference.claim_id}/approval-pack/manifest/policy_document/sources",
        json={"sources": [{"kind": "document", "id": reference.document_ids["policy"]}]},
        headers=_h(OUTSIDER),
    )
    assert denied.status_code == 403


def test_generation_refuses_stale_or_missing_inputs_without_partial_artifact(tmp_path):
    env = _build(tmp_path, "refuse", model=_model())
    reference = _seed_reference(env, suffix="F")
    assert reference.readiness["ready"] is True

    # Material input changes after the card was read.
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE chase_checklists SET status = 'open' WHERE claim_id = :claim_id"),
            {"claim_id": reference.claim_id},
        )
    response = _generate(env, reference)
    assert response.status_code == 409
    assert response.json()["code"] in {"READINESS_STALE", "PACK_NOT_READY"}
    assert response.json()["readiness"]["ready"] is False
    assert _events(env.app, "pack.merged", reference.claim_id) == []
    assert _drafts(env.app, reference.claim_id) == []

    # Refusal is visible/idempotent but never produces a partial pack.
    response_2 = _generate(env, reference)
    assert response_2.status_code == 409
    assert len(_events(env.app, "pack.generation_refused", reference.claim_id)) == 1
    assert env.renderer.calls == []


# --- Reference merge, deterministic versions, two AR-2 gates ------------------------


def test_reference_claim_merges_in_manifest_order_with_bookmarks_sha_and_ledger(tmp_path):
    model = _model(gnote=[_g_note_clean()])
    env = _build(tmp_path, "reference", model=model)
    reference = _seed_reference(env, suffix="M")
    assert reference.readiness["ready"] is True, reference.readiness["blockers"]
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    response = _generate(env, reference)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "ready_for_note_review"
    assert result["pack_version"] == 1
    assert result["note_status"] == "in_review"

    merged_events = _events(env.app, "pack.merged", reference.claim_id)
    assert len(merged_events) == 1
    payload = merged_events[0]["payload"]
    assert payload["version"] == 1
    assert payload["filename"] == "All Docs merged for KBX 123A.pdf"
    assert payload["readiness_fingerprint"] == reference.readiness["fingerprint"]
    assert [item["item_id"] for item in payload["manifest"]] == [
        "policy_document", "intimation_email", "claim_form", "logbook",
        "driving_licence", "kra_pin_cert", "photos", "repair_estimate",
        "assessor_engagement_email", "assessor_report", "supplier_quotes",
        "assessor_payment_request", "claim_details_report",
    ]
    assert len(payload["manifest"][6]["sources"]) == 3
    assert len(payload["manifest"][10]["sources"]) == 2
    assert payload["object_lock_status"] in {"local_write_once", "s3_object_lock"}

    merged = env.app.state.blob_store.get(payload["blob_key"])
    assert hashlib.sha256(merged).hexdigest() == payload["sha256"]
    assert payload["blob_key"].endswith(f"/{payload['sha256']}.pdf")

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(merged))
    outline = [entry for entry in reader.outline if not isinstance(entry, list)]
    assert [entry.title for entry in outline] == [
        item["label"] for item in payload["manifest"]
    ]
    for entry, item in zip(outline, payload["manifest"], strict=True):
        expected_zero_based = item["sources"][0]["pack_pages"][0] - 1
        assert reader.get_destination_page_number(entry) == expected_zero_based

    cover = reader.pages[0].extract_text()
    for heading in ("Item", "Source document", "Received date", "Pages"):
        assert heading in cover
    for item in payload["manifest"]:
        assert item["label"] in cover

    # Three photos => two pages; every required caption survives text extraction.
    photo_sources = payload["manifest"][6]["sources"]
    photo_pages = sorted({page for source in photo_sources for page in source["pack_pages"]})
    assert len(photo_pages) == 2
    photo_text = "\n".join(reader.pages[number - 1].extract_text() for number in photo_pages)
    for filename in ("damage-front.png", "damage-rear.png", "damage-side.png"):
        assert filename in photo_text
    assert "received 2026-07-22 EAT" in photo_text

    _drain(env.app)
    ledger = _rows(
        env.app,
        "SELECT action, claim_id, detail FROM audit_ledger "
        "WHERE claim_id = :claim_id AND action = 'pack.merged'",
        claim_id=reference.claim_id,
    )
    assert len(ledger) == 1
    assert payload["sha256"] in json.dumps(ledger[0]["detail"])


def test_l1_stages_each_capability_and_same_idempotency_key_never_duplicates(tmp_path):
    env = _build(tmp_path, "gates", model=_model())
    reference = _seed_reference(env, suffix="G")
    assert reference.readiness["ready"] is True

    first = _generate(env, reference, key="stable-key")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "staged"
    assert first.json()["capability_id"] == "pack.merge"
    assert _events(env.app, "pack.merged", reference.claim_id) == []
    assert _drafts(env.app, reference.claim_id) == []
    assert env.renderer.calls == []
    commentary_calls = [
        call
        for call in env.model.calls
        if call["inputs"].get("task") == "pack_note_commentary"
    ]
    assert commentary_calls == []

    replay = _generate(env, reference, key="stable-key")
    assert replay.status_code == 202
    assert replay.json()["review_item_id"] == first.json()["review_item_id"]
    merge_drafts = [
        item for item in _reviews(env.app, reference.claim_id, "DRAFT_RELEASE")
        if item["payload"].get("capability_id") == "pack.merge"
    ]
    assert len(merge_drafts) == 1

    # The public resolver releases merge; note remains a separately staged L1 action.
    resolution = env.client.post(
        f"/reviews/{first.json()['review_item_id']}/resolve",
        json={
            "action": "approve",
            "schema_version": "DRAFT_RELEASE@1",
            "payload": {
                "capability_id": "pack.merge",
                "diff": {"typed_changes": [], "prose_change_ratio": 0},
            },
        },
        headers=_h(),
    )
    assert resolution.status_code == 200, resolution.text
    _drain(env.app)
    assert len(_events(env.app, "pack.merged", reference.claim_id)) == 1
    assert _drafts(env.app, reference.claim_id) == []
    note_stages = [
        item for item in _reviews(env.app, reference.claim_id, "DRAFT_RELEASE")
        if item["payload"].get("capability_id") == "pack.note_draft"
    ]
    assert len(note_stages) == 1


def test_html_timeout_uses_one_plaintext_fallback_and_marks_only_that_source(tmp_path):
    renderer = FakeHtmlRenderer()
    renderer.fail_at = {1}
    env = _build(tmp_path, "fallback", model=_model(), renderer=renderer)
    reference = _seed_reference(env, suffix="H")
    _set_level(env, "pack.merge", "L3")
    # Keep note L1: this test isolates conversion and avoids a G-NOTE call.

    response = _generate(env, reference)
    assert response.status_code == 201, response.text
    payload = _events(env.app, "pack.merged", reference.claim_id)[0]["payload"]
    sources = [
        source
        for item in payload["manifest"]
        for source in item["sources"]
        if source["kind"] == "communication"
    ]
    fallbacks = [source for source in sources if source["fallback_used"]]
    assert len(fallbacks) == 1
    assert fallbacks[0]["fallback_reason"] == "html_renderer_timeout"
    assert all(source["blocked_resource_count"] >= 1 for source in sources)
    # Three communication sources: one timeout call + two successful calls.
    assert len(renderer.calls) == 3
    assert all("https://tracking.invalid" in call.html for call in renderer.calls)


# --- T-01 integrity, G-NOTE red team, FSM + fail-closed resolution -------------------


def test_t01_values_are_exact_locked_cited_and_blocked_slots_never_gain_numbers(tmp_path):
    model = _model(gnote=[_g_note_clean()])
    env = _build(tmp_path, "note", model=model)
    reference = _seed_reference(env, suffix="N")
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    response = _generate(env, reference)
    assert response.status_code == 201, response.text

    drafts = _drafts(env.app, reference.claim_id)
    assert len(drafts) == 1
    row = drafts[0]
    assert row["version"] == 1 and row["status"] == "in_review"
    assert row["edited_by"] is None and row["signed_by"] is None and row["signed_at"] is None
    body = row["body"]
    assert body["schema_version"] == 1
    assert body["template_id"] == "T-01"
    assert body["merged_pack"]["version"] == 1
    assert body["signable"] is False
    assert body["integrity"]["g_tpl_result"] == "pass"
    assert body["integrity"]["g_note_result"] == "pass"

    assert [section["template_slot"] for section in body["sections"]] == [
        "computed", "verification", "incident_summary",
        "excess_vs_max", "savings_narrative",
    ]
    assert body["sections"][0]["locked"] is True
    assert body["sections"][1]["locked"] is True
    assert all(section["locked"] is False for section in body["sections"][2:])
    computed = {
        slot["slot"]: slot for slot in body["sections"][0]["content"]
    }
    assert computed["assessed_amount"]["value"] == ASSESSED
    assert computed["estimate"]["value"] == ESTIMATE
    assert computed["excess"]["value"] == EXCESS
    assert computed["pav"]["value"] == PAV
    assert computed["excess_protector"]["value"] is False
    for slot in ("assessed_amount", "estimate", "excess", "pav", "loss_location"):
        assert computed[slot]["source_ref"]
        assert computed[slot]["citation_marker"]
    assert computed["amount_payable"]["display"] == "PENDING CAPTURE"
    assert "value" not in computed["amount_payable"]
    for slot in (
        "repair_amount", "percent_si", "percent_pav", "garage",
        "third_party_count", "duty_paid", "recovery_register_flag", "subrogation",
    ):
        assert computed[slot]["state"] == "blocked_on_inputs"
        assert "value" not in computed[slot]

    calls = [call for call in model.calls if call["inputs"].get("task") == "pack_note_commentary"]
    assert len(calls) == 1
    assert calls[0]["tier"] == "MODEL_HEAVY"
    inputs = calls[0]["inputs"]
    serialised = json.dumps(inputs, sort_keys=True)
    assert "document_text" not in serialised
    assert "body_s3_key" not in serialised
    assert "broker@example.co.ke" not in serialised
    assert set(inputs["verified_fields"]) == set(ACTIVE_FIELDS)
    assert inputs["savings_rows"][0]["id"] == "SAVE-HEADER"
    assert inputs["savings_rows"][0]["saving"] == SAVING

    reviews = _reviews(env.app, reference.claim_id, "NOTE_REVIEW")
    assert len(reviews) == 1
    assert reviews[0]["subtype"] == "approval_note"
    assert reviews[0]["status"] == "open"
    assert reviews[0]["payload"]["note_draft_id"] == row["id"]
    assert reviews[0]["payload"]["merged_pack_event_id"] == body["merged_pack"]["event_id"]
    claim = env.client.get(f"/claims/{reference.claim_id}", headers=_h())
    assert claim.status_code == 200 and claim.json()["status"] == "PACK_READY"
    assert _events(env.app, "pack.route", reference.claim_id) == []

    # Packet 18 has no usable sign/reject path.
    snapshot = json.dumps(_drafts(env.app, reference.claim_id), sort_keys=True, default=str)
    for action in ("approve", "edit_approve", "reject"):
        blocked = env.client.post(
            f"/reviews/{reviews[0]['id']}/resolve",
            json={
                "action": action,
                "schema_version": "NOTE_REVIEW@1",
                "payload": {
                    "capability_id": "pack.note_draft",
                    "diff": {"typed_changes": [], "prose_change_ratio": 0},
                    "reason": "Packet 19 owns this action",
                },
            },
            headers=_h(),
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "NOTE_REVIEW_UI_NOT_BUILT"
    assert json.dumps(_drafts(env.app, reference.claim_id), sort_keys=True, default=str) == snapshot
    assert _reviews(env.app, reference.claim_id, "NOTE_REVIEW")[0]["status"] == "open"


def test_g_note_catches_injected_number_and_model_token_omission(tmp_path):
    model = _model(gnote=[_g_note_clean(), _g_note_injected(), _g_note_clean()])
    env = _build(tmp_path, "gnote", model=model)
    reference = _seed_reference(env, suffix="X")
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    assert _generate(env, reference).status_code == 201

    rendered = _events(env.app, "template.rendered", reference.claim_id)
    candidate = next(event for event in rendered if event["payload"]["template_id"] == "T-01")
    original = env.app.state.blob_store.get(candidate["payload"]["blob_key"])
    assert b"124,724" in original
    tampered = original.replace(b"124,724", b"999,999", 1)
    injected_key = f"tests/red-team/{reference.claim_id}/injected.html"
    env.app.state.blob_store.put(injected_key, tampered)

    subject = {
        "claim_id": reference.claim_id,
        "template_id": "T-01",
        "blob_key": injected_key,
        "signable": False,
    }
    injected = env.app.state.eval_harness.grade("G-NOTE", subject, actor="agent:eval")
    assert injected.result == "fail"
    assert injected.detail["code"] in {"NUMERIC_SOURCE_MISMATCH", "UNSUPPORTED_NUMBER"}

    # Model omits the injected token: deterministic multiset comparison still fails.
    omitted = env.app.state.eval_harness.grade("G-NOTE", subject, actor="agent:eval")
    assert omitted.result == "fail"
    assert omitted.detail["code"] == "NUMERIC_TOKEN_OMISSION"


def test_commentary_fails_twice_creates_four_part_exception_without_pack_ready(tmp_path):
    bad = {
        "paragraphs": [
            {
                "template_slot": "incident_summary",
                "content": "The vehicle was travelling at 999 kilometres per hour.",
                "numbers_used": ["999"],
            },
            {"template_slot": "excess_vs_max", "content": "No comment.", "numbers_used": []},
            {"template_slot": "savings_narrative", "content": "No comment.", "numbers_used": []},
        ]
    }
    model = _model(commentary=[bad, bad], gnote=[])
    env = _build(tmp_path, "bad-commentary", model=model)
    reference = _seed_reference(env, suffix="B")
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    response = _generate(env, reference)
    assert response.status_code in {201, 409}
    _drain(env.app)

    calls = [call for call in model.calls if call["inputs"].get("task") == "pack_note_commentary"]
    assert len(calls) == 2
    assert "validation_errors" not in calls[0]["inputs"]
    assert calls[1]["inputs"]["validation_errors"]
    assert _events(env.app, "pack.merged", reference.claim_id)
    assert _drafts(env.app, reference.claim_id) == []
    assert _reviews(env.app, reference.claim_id, "NOTE_REVIEW") == []
    exceptions = [
        item for item in _reviews(env.app, reference.claim_id, "EXCEPTION")
        if item["subtype"] == "note_commentary_invalid"
    ]
    assert len(exceptions) == 1
    assert {"facts", "risk", "recommendation", "resolution_schema"} <= set(
        exceptions[0]["payload"]
    )
    claim = env.client.get(f"/claims/{reference.claim_id}", headers=_h())
    assert claim.status_code == 200 and claim.json()["status"] == "RESERVED"


def test_regeneration_retains_bytes_and_supersedes_only_unsigned_note(tmp_path):
    model = _model(
        commentary=[_commentary(), _commentary()],
        gnote=[_g_note_clean(), _g_note_clean()],
    )
    env = _build(tmp_path, "versions", model=model)
    reference = _seed_reference(env, suffix="V")
    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")

    first = _generate(env, reference, key="version-one")
    assert first.status_code == 201
    first_event = _events(env.app, "pack.merged", reference.claim_id)[0]
    first_bytes = env.app.state.blob_store.get(first_event["payload"]["blob_key"])

    card = env.client.get(
        f"/claims/{reference.claim_id}/approval-pack/readiness", headers=_h()
    )
    assert card.status_code == 200
    assert card.json()["status"] == "PACK_READY"
    assert card.json()["ready"] is True
    reference.readiness = card.json()

    second = _generate(env, reference, key="version-two")
    assert second.status_code == 201, second.text
    events = _events(env.app, "pack.merged", reference.claim_id)
    assert [event["payload"]["version"] for event in events] == [1, 2]
    second_bytes = env.app.state.blob_store.get(events[1]["payload"]["blob_key"])
    # Fixed time + identical sources/render fakes => exact deterministic bytes.
    assert second_bytes == first_bytes
    assert events[1]["payload"]["sha256"] == events[0]["payload"]["sha256"]
    assert events[1]["payload"]["blob_key"] != events[0]["payload"]["blob_key"]

    drafts = _drafts(env.app, reference.claim_id)
    assert [(row["version"], row["status"]) for row in drafts] == [
        (1, "superseded"), (2, "in_review"),
    ]
    assert drafts[0]["body"]["merged_pack"]["version"] == 1
    assert drafts[1]["body"]["merged_pack"]["version"] == 2

    # A replay of version-two's key returns its ids and appends nothing.
    replay = _generate(env, reference, key="version-two")
    assert replay.status_code == 201
    assert replay.json()["pack_event_id"] == second.json()["pack_event_id"]
    assert len(_events(env.app, "pack.merged", reference.claim_id)) == 2
    assert len(_drafts(env.app, reference.claim_id)) == 2


def test_no_payment_or_approval_authority_side_effect_is_added():
    # Static constitution pin: approval authority is not a capability and this
    # packet adds no funds-transfer operation or settlement action.
    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8")
    )
    ids = {row["id"] for row in policies["capabilities"]}
    assert not any(value.startswith("approval.") for value in ids)
    assert "pack.route" in ids  # matrix lookup only; PACKET-19 executes routing.
    repo_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((REPO / "agents" / "approval_pack_agent").glob("*.py"))
    )
    for forbidden in ("funds_transfer", "transfer_funds", "execute_payment"):
        assert forbidden not in repo_text
