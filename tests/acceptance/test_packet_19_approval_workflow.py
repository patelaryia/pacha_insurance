"""PACKET-19 acceptance — PRD-08 approval-note signing, routing, and S-3.

Protected (CODEOWNERS): the builder may not weaken this file once merged.
Contract per docs/packets/PACKET-19_approval_workflow.md §10. No live browser,
S3, Graph, ICON, network call, or provider model is permitted: HTML rendering
and both MODEL_HEAVY tasks use strict injected fakes and time is fixed.

The production motor pack is deliberately unsignable — C-08 is uncaptured and
T-03 is `pending_capture`. Every executable mechanic below is therefore proved
twice: refused on the live pack, and exercised on a synthetic fixture pack that
is explicitly *not* the production configuration (§0).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from claim_core import ClaimCoreError

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

OFFICER = "user:01HP19OFFICER00000000AAAAA"
MANAGER = "user:01HP19MANAGER00000000AAAAA"
MD = "user:01HP19MANAGINGDIR0000AAAAA"
CHAIRMAN = "user:01HP19CHAIRMAN0000000AAAAA"
GM = "user:01HP19GENERALMGR00000AAAAA"
HEAD_OF_CLAIMS = "user:01HP19HEADOFCLAIMS000AAAAA"
OUTSIDER = "user:01HP19OUTSIDER0000000AAAAA"
ROLES = {
    OFFICER: "claims_officer",
    MANAGER: "claims_manager",
    MD: "md",
    CHAIRMAN: "chairman",
    GM: "gm",
    HEAD_OF_CLAIMS: "head_of_claims",
    OUTSIDER: "auditor",
}

T0 = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)  # 11:00 EAT

ESTIMATE = 26_100_000
ASSESSED = 13_627_600
PAV = 150_000_000
SUM_INSURED = 60_000_000
EXCESS = 1_500_000
SAVING = 12_472_400
RESERVE_TOTAL = 14_000_000
FOUR_MILLION = 4_000_000_00

ACTIVE_FIELDS: dict[str, tuple[Any, str]] = {
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
CONSISTENCY_CHECKS = ("CC-1", "CC-2", "CC-4", "CC-5")


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
        if len(self.calls) in self.fail_at:
            raise TimeoutError("synthetic chromium timeout")
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
        template = queue[0] if len(queue) == 1 else queue.pop(0)
        value = json.loads(json.dumps(template))
        if str(task) == "g_note_grade":
            # The savings row is per claim, so the fake grader cites the row the
            # claim actually owns. Every other assertion stays deterministic.
            for numeric_claim in value.get("numeric_claims", []):
                if numeric_claim.get("source_kind") == "savings_ledger":
                    numeric_claim["source_ref"] = _savings_id(str(inputs["_claim_id"]))
        return {
            "data": value,
            "cost_usd": 0.01,
            "model_id": f"fake-{tier.casefold()}",
        }


def _savings_id(claim_id: str) -> str:
    return f"SAVE-{claim_id}"


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
    text_value: str, *, source_kind: str, source_ref: str, observed_value: int | str
) -> dict[str, Any]:
    return {
        "text": text_value,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "observed_value": observed_value,
        "value_type": "money",
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
                source_ref="SAVE-PLACEHOLDER", observed_value=SAVING,
            ),
        ],
        "unsupported_assertions": [],
        "missing_sections": [],
        "tone_ok": True,
    }


def _model() -> TaskModel:
    # A single scripted response per task is reused: PACKET-19 grades the same
    # commentary again at sign time, and the note is regenerated on rejection.
    return TaskModel(
        {
            "pack_note_commentary": [_commentary()],
            "g_note_grade": [_g_note_clean()],
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
        return [{key: _decode(value) for key, value in dict(row).items()} for row in rows]


def _events(app: Any, type_: str, claim_id: str | None = None) -> list[dict[str, Any]]:
    rows = _rows(
        app,
        "SELECT id, claim_id, type, payload, actor, correlation_id FROM events "
        "WHERE type = :type ORDER BY seq",
        type=type_,
    )
    return rows if claim_id is None else [row for row in rows if row["claim_id"] == claim_id]


def _drafts(app: Any, claim_id: str) -> list[dict[str, Any]]:
    return _rows(
        app,
        "SELECT id, version, body, status, edited_by, signed_by, signed_at "
        "FROM note_drafts WHERE claim_id = :claim_id ORDER BY version",
        claim_id=claim_id,
    )


def _reviews(app: Any, claim_id: str, type_: str) -> list[dict[str, Any]]:
    app.state.review_queue.backfill("agent:test")
    return _rows(
        app,
        "SELECT id, type, subtype, status, payload, resolution FROM review_items "
        "WHERE claim_id = :claim_id AND type = :type ORDER BY created_at, id",
        claim_id=claim_id,
        type=type_,
    )


def _drain(app: Any, cycles: int = 256) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


# --- synthetic fixture pack ---------------------------------------------------------


def _fixture_pack(tmp_path: pathlib.Path, *, t03_live: bool) -> pathlib.Path:
    """Return a synthetic pack whose T-01 is fully captured.

    This is explicitly not the production configuration: it exists so the
    executable sign/route mechanics can be proved without inventing C-08, the
    nine uncaptured T-01 slots, the driver party-match producer, or a T-03 body.
    """

    pack = tmp_path / "fixture-pack" / "motor"
    pack.parent.mkdir(parents=True, exist_ok=True)
    if not pack.exists():
        shutil.copytree(MOTOR_PACK, pack)

    note = yaml.safe_load((pack / "approval_pack" / "note.yaml").read_text("utf-8"))
    substitutes = {
        "amount_payable": ("assessment.agreed_quote", "money"),
        "repair_amount": ("assessment.agreed_quote", "money"),
        "percent_si": ("policy.sum_insured", "money"),
        "percent_pav": ("assessment.pav", "money"),
        "garage": ("loss.location", "string"),
        "third_party_count": ("loss.narrative", "string"),
        "duty_paid": ("policy.excess_protector", "bool"),
        "recovery_register_flag": ("policy.excess_protector", "bool"),
        "subrogation": ("policy.excess_protector", "bool"),
    }
    for slot_id, (path, value_type) in substitutes.items():
        note["slots"][slot_id] = {
            "label": note["slots"][slot_id]["label"],
            "locked": True,
            "status": "active",
            "source": "claim_field",
            "field_path": path,
            "value_type": value_type,
        }
    note["verification"]["driver_is_insured"] = {
        "label": "Driver is the insured",
        "locked": True,
        "check_ids": ["CC-2"],
    }
    (pack / "approval_pack" / "note.yaml").write_text(
        yaml.safe_dump(note, sort_keys=False), encoding="utf-8"
    )

    if t03_live:
        registry = yaml.safe_load((pack / "templates" / "registry.yaml").read_text("utf-8"))
        body = pack / "templates" / "templates" / "T-03-fixture.j2"
        body.write_text(
            "<html><body><h1>Alert</h1><p>{{ vehicle_reg }}</p></body></html>",
            encoding="utf-8",
        )
        for entry in registry["templates"]:
            if entry["id"] == "T-03":
                entry["status"] = "live"
                entry["body_ref"] = "templates/T-03-fixture.j2"
                entry["required_fields"] = ["vehicle.reg"]
                entry.pop("blocked_on", None)
        (pack / "templates" / "registry.yaml").write_text(
            yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
        )
    return pack


class Env:
    def __init__(self, app, client, clock, renderer, model, pack) -> None:
        self.app = app
        self.client = client
        self.clock = clock
        self.renderer = renderer
        self.model = model
        self.pack = pack


def _build(
    tmp_path: pathlib.Path,
    name: str,
    *,
    model: TaskModel | None = None,
    pack: pathlib.Path | None = None,
) -> Env:
    # I001 suppressed: package becomes first-party only after implementation.
    from fastapi.testclient import TestClient  # noqa: I001

    from agent_runtime import build_agent_runtime
    from approval_pack_agent import build_approval_pack_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    model = model or _model()
    clock = FixedClock()
    renderer = FakeHtmlRenderer()
    pack = pack or MOTOR_PACK
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(database_url, clock=clock)
    build_cop_runtime(app, pack_paths=[pack])
    build_eval_harness(app, model_client=model)
    build_review_queue(app, roles=dict(ROLES))
    build_agent_runtime(app)
    build_approval_pack_agent(
        app, model_client=model, html_renderer=renderer, pack_root=pack
    )
    return Env(app, TestClient(app), clock, renderer, model, pack)


def _create_reserved(env: Env, suffix: str = "A") -> str:
    response = env.client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.0.0"}, headers=_h()
    )
    assert response.status_code == 201, response.text
    claim_id = response.json()["id"]
    with env.app.state.engine.begin() as connection:
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
            {"id": f"CHECKLIST-{suffix}", "claim_id": claim_id, "created_at": T0},
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


def _write_fields(env: Env, claim_id: str, extra: dict[str, tuple[Any, str]]) -> None:
    writes = [
        {
            "path": path,
            "value": value,
            "value_type": value_type,
            "source_type": "human",
            "source_ref": {"user_id": OFFICER},
            "verification_state": "human_verified",
        }
        for path, (value, value_type) in {**ACTIVE_FIELDS, **extra}.items()
    ]
    response = env.client.patch(
        f"/claims/{claim_id}/fields", json={"writes": writes}, headers=_h()
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
                "UPDATE documents SET doc_type = :doc_type, status = 'verified', "
                "page_count = :page_count WHERE id = :id"
            ),
            {"id": row.id, "doc_type": doc_type, "page_count": page_count},
        )
    return row.id


def _add_communication(env: Env, claim_id: str, purpose: str, sequence: int) -> str:
    body = (
        f"<html><body><h1>{purpose}</h1>"
        "<p>Archived claim correspondence.</p></body></html>"
    )
    row, created = env.app.state.claim_service.record_inbound_communication(
        graph_message_id=f"msg-p19-{claim_id[-5:]}-{sequence}",
        claim_id=claim_id,
        thread_id=f"thread-{claim_id}",
        from_addr="broker@example.co.ke",
        to_addrs=["claims@mayfair.co.ke"],
        subject=purpose,
        body_text=body,
    )
    assert created
    return row.id


def _select(env: Env, claim_id: str, item_id: str, sources: list[dict[str, str]]) -> None:
    response = env.client.put(
        f"/claims/{claim_id}/approval-pack/manifest/{item_id}/sources",
        json={"sources": sources},
        headers=_h(),
    )
    assert response.status_code == 200, response.text


def _upload(env: Env, claim_id: str, item_id: str, filename: str) -> None:
    response = env.client.post(
        f"/claims/{claim_id}/approval-pack/manifest/{item_id}/upload",
        files={"file": (filename, _pdf(filename), "application/pdf")},
        headers=_h(),
    )
    assert response.status_code == 201, response.text


def _set_level(env: Env, capability_id: str, level: str) -> None:
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
            {"level": level, "id": capability_id},
        )


@dataclass
class Claim:
    id: str
    readiness: dict[str, Any]
    review_id: str
    draft_id: str
    body_sha256: str


def _seed(
    env: Env,
    *,
    suffix: str = "A",
    reserve_total: int | None = RESERVE_TOTAL,
    consistency: bool = True,
) -> Claim:
    """Seed one claim through PACKET-18 to an open NOTE_REVIEW at PACK_READY."""

    claim_id = _create_reserved(env, suffix)
    extra: dict[str, tuple[Any, str]] = {}
    if reserve_total is not None:
        extra["reserve.total"] = (reserve_total, "money")
    _write_fields(env, claim_id, extra)

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
        "estimate": _add_document(
            env, claim_id, filename="estimate.pdf",
            content=_pdf("REPAIR ESTIMATE"), doc_type="repair_estimate",
        ),
        "report": _add_document(
            env, claim_id, filename="assessor-report.pdf",
            content=_pdf("ASSESSOR REPORT"), doc_type="assessor_report",
        ),
        "supplier_quote": _add_document(
            env, claim_id, filename="supplier-breakdown.pdf",
            content=_pdf("SUPPLIER BREAKDOWN"), doc_type=None,
        ),
    }
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
    _upload(env, claim_id, "assessor_payment_request", "assessor-payment.pdf")
    _upload(env, claim_id, "claim_details_report", "claim-details.pdf")

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO savings_ledger "
                "(id, claim_id, kind, baseline_amount, achieved_amount, evidence, "
                "vendor_id, occurred_at) VALUES "
                "(:savings_id, :claim_id, 'assessment_negotiation', :baseline, "
                ":achieved, :evidence, NULL, :occurred_at)"
            ),
            {
                "savings_id": _savings_id(claim_id),
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
        if consistency:
            for index, check_id in enumerate(CONSISTENCY_CHECKS):
                connection.execute(
                    text(
                        "INSERT INTO consistency_results (id, claim_id, check_id, "
                        "status, severity, evidence, rationale, created_at) "
                        "VALUES (:id, :claim_id, :check_id, 'pass', 'minor', "
                        ":evidence, :rationale, :created_at)"
                    ),
                    {
                        "id": f"CC-{suffix}-{index}",
                        "claim_id": claim_id,
                        "check_id": check_id,
                        "evidence": json.dumps({"fixture": True}),
                        "rationale": f"{check_id} fixture evidence",
                        "created_at": T0,
                    },
                )

    readiness = env.client.get(
        f"/claims/{claim_id}/approval-pack/readiness", headers=_h()
    )
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is True, readiness.json()["blockers"]

    _set_level(env, "pack.merge", "L3")
    _set_level(env, "pack.note_draft", "L3")
    generated = env.client.post(
        f"/claims/{claim_id}/approval-pack/generate",
        json={"readiness_fingerprint": readiness.json()["fingerprint"]},
        headers={**_h(), "Idempotency-Key": f"generate-{suffix}"},
    )
    assert generated.status_code == 201, generated.text
    _drain(env.app)
    reviews = _reviews(env.app, claim_id, "NOTE_REVIEW")
    assert len(reviews) == 1 and reviews[0]["status"] == "open"
    workspace = env.client.get(
        f"/reviews/{reviews[0]['id']}/approval-note", headers=_h()
    )
    assert workspace.status_code == 200, workspace.text
    return Claim(
        id=claim_id,
        readiness=readiness.json(),
        review_id=reviews[0]["id"],
        draft_id=workspace.json()["current_draft"]["id"],
        body_sha256=workspace.json()["current_draft"]["body_sha256"],
    )


def _workspace(env: Env, claim: Claim, actor: str = OFFICER) -> dict[str, Any]:
    response = env.client.get(
        f"/reviews/{claim.review_id}/approval-note", headers=_h(actor)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _autosave(
    env: Env,
    claim: Claim,
    *,
    base_draft_id: str,
    base_body_sha256: str,
    contents: dict[str, str] | None = None,
    key: str = "save-1",
    actor: str = OFFICER,
) -> Any:
    contents = contents or {}
    current = _workspace(env, claim, actor)
    sections = {
        section["template_slot"]: section
        for section in current["current_draft"]["body"]["sections"]
    }
    return env.client.put(
        f"/reviews/{claim.review_id}/approval-note/draft",
        json={
            "base_draft_id": base_draft_id,
            "base_body_sha256": base_body_sha256,
            "commentary": [
                {
                    "template_slot": slot,
                    "content": contents.get(slot, sections[slot]["content"]),
                }
                for slot in current["commentary_slots"]
            ],
        },
        headers={**_h(actor), "Idempotency-Key": key},
    )


def _resolve(
    env: Env, review_id: str, action: str, schema: str, payload: dict[str, Any],
    actor: str = OFFICER,
) -> Any:
    return env.client.post(
        f"/reviews/{review_id}/resolve",
        json={"action": action, "schema_version": schema, "payload": payload},
        headers=_h(actor),
    )


def _sign_payload(draft_id: str, body_sha256: str) -> dict[str, Any]:
    return {
        "capability_id": "pack.note_draft",
        "draft_id": draft_id,
        "body_sha256": body_sha256,
        "diff": {"typed_changes": [], "prose_change_ratio": 0},
    }


@pytest.fixture
def live(tmp_path):
    """The production motor pack: C-08 uncaptured, T-03 pending_capture."""

    return _build(tmp_path, "live")


@pytest.fixture
def fixture_env(tmp_path):
    """A synthetic pack whose T-01 is captured and whose T-03 is not."""

    return _build(tmp_path, "fixture", pack=_fixture_pack(tmp_path, t03_live=False))


# --- 1. the live motor pack refuses to sign -----------------------------------------


def test_live_motor_note_refuses_sign_with_the_named_c08_blocker(live):
    claim = _seed(live, suffix="L")
    workspace = _workspace(live, claim)

    assert workspace["signable"] is False
    blocked = {row["slot"]: row for row in workspace["blockers"]}
    assert blocked["amount_payable"]["state"] == "pending_capture"
    assert "C-08" in blocked["amount_payable"]["detail"]
    assert workspace["sign_state"] == "unsigned"

    before = json.dumps(_drafts(live.app, claim.id), sort_keys=True, default=str)
    refused = _resolve(
        live,
        claim.review_id,
        "approve",
        "NOTE_REVIEW@2",
        _sign_payload(claim.draft_id, claim.body_sha256),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "SIGN_BLOCKED_ON_INPUTS"
    assert any(
        row["slot"] == "amount_payable" for row in refused.json()["blockers"]
    )
    _drain(live.app)

    # Nothing moved: no signature, no route, no review, no FSM hop.
    assert json.dumps(_drafts(live.app, claim.id), sort_keys=True, default=str) == before
    assert _events(live.app, "pack.note_sign_prepared", claim.id) == []
    assert _events(live.app, "pack.note_signed", claim.id) == []
    assert _events(live.app, "pack.routed", claim.id) == []
    assert _reviews(live.app, claim.id, "PACK_REVIEW") == []
    assert _reviews(live.app, claim.id, "NOTE_REVIEW")[0]["status"] == "open"
    status = live.client.get(f"/claims/{claim.id}", headers=_h())
    assert status.json()["status"] == "PACK_READY"


# --- 2. append-version autosave ------------------------------------------------------


def test_autosave_appends_versions_retains_history_and_rejects_a_stale_tab(fixture_env):
    claim = _seed(fixture_env, suffix="S")
    first = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents={"incident_summary": "The insured reported a junction collision."},
        key="save-1",
    )
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved["version"] == 2
    assert saved["parent_draft_id"] == claim.draft_id
    assert saved["recorded"] is True

    drafts = _drafts(fixture_env.app, claim.id)
    assert [(row["version"], row["status"]) for row in drafts] == [
        (1, "superseded"), (2, "in_review"),
    ]
    # History is retained verbatim: version 1 still holds the generated prose.
    original = {
        section["template_slot"]: section for section in drafts[0]["body"]["sections"]
    }
    assert original["incident_summary"]["content"].startswith("The insured reported a collision")
    assert drafts[1]["edited_by"] == OFFICER
    assert drafts[1]["body"]["lineage"] == {
        "root_draft_id": claim.draft_id,
        "parent_draft_id": claim.draft_id,
        "review_id": claim.review_id,
    }
    # Commentary text never reaches an event or the ledger.
    autosaved = _events(fixture_env.app, "pack.note_autosaved", claim.id)
    assert len(autosaved) == 1
    assert "junction collision" not in json.dumps(autosaved[0]["payload"])
    assert autosaved[0]["payload"]["body_sha256"] == saved["body_sha256"]

    # The open review still points at the one lineage; it was not cancelled.
    reviews = _reviews(fixture_env.app, claim.id, "NOTE_REVIEW")
    assert len(reviews) == 1 and reviews[0]["status"] == "open"
    workspace = _workspace(fixture_env, claim)
    assert workspace["current_draft"]["id"] == saved["draft_id"]
    assert workspace["current_draft"]["version"] == 2

    # A second tab still holding version 1 is refused and writes nothing.
    stale = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        key="save-stale",
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "STALE_NOTE_DRAFT"
    assert stale.json()["current_draft_id"] == saved["draft_id"]
    assert stale.json()["current_body_sha256"] == saved["body_sha256"]
    assert len(_drafts(fixture_env.app, claim.id)) == 2


def test_one_idempotency_key_replays_exactly_once_and_conflicts_on_new_content(
    fixture_env,
):
    claim = _seed(fixture_env, suffix="I")
    payload = {"incident_summary": "A junction collision was reported by the insured."}
    first = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents=payload,
        key="stable",
    )
    assert first.status_code == 200
    replay = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents=payload,
        key="stable",
    )
    assert replay.status_code == 200
    assert replay.json()["draft_id"] == first.json()["draft_id"]
    assert replay.json()["recorded"] is False
    assert len(_drafts(fixture_env.app, claim.id)) == 2
    assert len(_events(fixture_env.app, "pack.note_autosaved", claim.id)) == 1

    conflict = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents={"incident_summary": "A different account of the same collision."},
        key="stable",
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_drafts(fixture_env.app, claim.id)) == 2


def test_autosave_waits_for_the_durable_sign_resolution_and_then_refuses(
    fixture_env, monkeypatch,
):
    claim = _seed(fixture_env, suffix="LOCK")
    current = _workspace(fixture_env, claim)
    sections = {
        section["template_slot"]: section
        for section in current["current_draft"]["body"]["sections"]
    }
    commentary = [
        {
            "template_slot": slot,
            "content": sections[slot]["content"],
        }
        for slot in current["commentary_slots"]
    ]
    signing = fixture_env.app.state.approval_pack_agent.signing
    original_prepare = signing.prepare_signature
    entered = Event()
    release = Event()

    def paused_prepare(review, payload, actor):
        entered.set()
        assert release.wait(timeout=5)
        return original_prepare(review, payload, actor)

    monkeypatch.setattr(signing, "prepare_signature", paused_prepare)
    queue = fixture_env.app.state.review_queue.service
    workspace = fixture_env.app.state.approval_pack_agent.workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolving = pool.submit(
            queue.resolve,
            claim.review_id,
            actor=OFFICER,
            action="approve",
            schema_version="NOTE_REVIEW@2",
            payload=_sign_payload(claim.draft_id, claim.body_sha256),
        )
        assert entered.wait(timeout=5)
        saving = pool.submit(
            workspace.autosave,
            claim.review_id,
            actor=OFFICER,
            idempotency_key="racing-save",
            base_draft_id=claim.draft_id,
            base_body_sha256=claim.body_sha256,
            commentary=commentary,
        )
        time.sleep(0.05)
        assert saving.done() is False
        release.set()
        assert resolving.result(timeout=5)["status"] == "resolved"
        with pytest.raises(ClaimCoreError) as refused:
            saving.result(timeout=5)
    assert refused.value.code == "ALREADY_RESOLVED"
    assert len(_drafts(fixture_env.app, claim.id)) == 1
    assert _events(fixture_env.app, "pack.note_autosaved", claim.id) == []


# --- 3. locked sections and injected numbers ----------------------------------------


def test_locked_section_tampering_and_an_injected_number_are_rejected(fixture_env):
    claim = _seed(fixture_env, suffix="T")

    # A locked slot is not an accepted commentary slot: the request is refused
    # outright rather than silently ignored.
    tampered = fixture_env.client.put(
        f"/reviews/{claim.review_id}/approval-note/draft",
        json={
            "base_draft_id": claim.draft_id,
            "base_body_sha256": claim.body_sha256,
            "commentary": [
                {"template_slot": "computed", "content": "KES 999,999"},
                {"template_slot": "excess_vs_max", "content": "No comment."},
                {"template_slot": "savings_narrative", "content": "No comment."},
            ],
        },
        headers={**_h(), "Idempotency-Key": "tamper"},
    )
    assert tampered.status_code == 422
    assert tampered.json()["code"] == "COMMENTARY_SLOTS_INVALID"

    injected = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents={"savings_narrative": "The recorded saving was KES 999,999."},
        key="inject",
    )
    assert injected.status_code == 422
    assert injected.json()["code"] == "COMMENTARY_INVALID"
    assert any("999999" in error for error in injected.json()["validation_errors"])
    assert len(_drafts(fixture_env.app, claim.id)) == 1

    # The locked computed section is copied server-side and is unchanged.
    body = _workspace(fixture_env, claim)["current_draft"]["body"]
    computed = next(
        section for section in body["sections"] if section["template_slot"] == "computed"
    )
    assert computed["locked"] is True
    assert {slot["slot"] for slot in computed["content"]} >= {"assessed_amount", "excess"}


# --- 4. sign the exact saved hash, crash-safe ---------------------------------------


def _sign(env: Env, claim: Claim, actor: str = OFFICER) -> dict[str, Any]:
    workspace = _workspace(env, claim, actor)
    assert workspace["signable"] is True, workspace["blockers"]
    response = _resolve(
        env,
        claim.review_id,
        "approve",
        "NOTE_REVIEW@2",
        _sign_payload(
            workspace["current_draft"]["id"], workspace["current_draft"]["body_sha256"]
        ),
        actor=actor,
    )
    assert response.status_code == 200, response.text
    return workspace


def test_sign_grades_the_exact_saved_hash_and_replay_produces_one_of_everything(
    fixture_env,
):
    claim = _seed(fixture_env, suffix="G")
    saved = _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        contents={"incident_summary": "The insured reported a junction collision."},
        key="edit-then-sign",
    )
    assert saved.status_code == 200

    # A stale id/hash never signs, even when the review is open.
    stale = _resolve(
        fixture_env,
        claim.review_id,
        "approve",
        "NOTE_REVIEW@2",
        _sign_payload(claim.draft_id, claim.body_sha256),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "STALE_NOTE_DRAFT"
    assert _events(fixture_env.app, "pack.note_sign_prepared", claim.id) == []

    workspace = _sign(fixture_env, claim)
    signed_draft_id = workspace["current_draft"]["id"]
    signed_hash = workspace["current_draft"]["body_sha256"]
    prepared = _events(fixture_env.app, "pack.note_sign_prepared", claim.id)
    assert len(prepared) == 1
    assert prepared[0]["payload"]["note_draft_id"] == signed_draft_id
    assert prepared[0]["payload"]["body_sha256"] == signed_hash
    assert prepared[0]["payload"]["integrity"]["g_tpl_result"] == "pass"
    assert prepared[0]["payload"]["integrity"]["g_note_result"] == "pass"

    # Before the durable consumer runs, the resolution is never reported as lost.
    pending = _workspace(fixture_env, claim)
    assert pending["sign_state"] == "signing_pending"

    _drain(fixture_env.app)
    # Replay the same durable event: finalisation is idempotent.
    for event in _events(fixture_env.app, "review.resolved", claim.id):
        fixture_env.app.state.approval_pack_agent.consume(
            SimpleNamespace(
                type="review.resolved",
                payload=event["payload"],
                actor=event["actor"],
                claim_id=claim.id,
                id=event["id"],
            )
        )
    _drain(fixture_env.app)

    signed = _events(fixture_env.app, "pack.note_signed", claim.id)
    routed = _events(fixture_env.app, "pack.routed", claim.id)
    assert len(signed) == 1
    assert len(routed) == 1
    assert signed[0]["payload"]["body_sha256"] == signed_hash
    assert signed[0]["payload"]["signed_by"] == OFFICER
    assert signed[0]["payload"]["prepared_event_id"] == prepared[0]["id"]

    drafts = {row["id"]: row for row in _drafts(fixture_env.app, claim.id)}
    assert drafts[signed_draft_id]["status"] == "signed"
    assert drafts[signed_draft_id]["signed_by"] == OFFICER
    assert drafts[signed_draft_id]["signed_at"] is not None
    assert drafts[claim.draft_id]["status"] == "superseded"

    packs = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")
    assert len(packs) == 1
    assert packs[0]["subtype"] == "approval_pack"
    assert packs[0]["payload"]["required_role"] == "claims_manager"
    assert packs[0]["payload"]["routing_amount_cents"] == RESERVE_TOTAL
    assert packs[0]["payload"]["route_provenance"]["source"] == "claim_field"
    assert packs[0]["payload"]["route_provenance"]["path"] == "reserve.total"

    transitions = [
        event
        for event in _events(fixture_env.app, "claim.status_changed", claim.id)
        if event["payload"].get("to") == "IN_APPROVAL"
    ]
    assert len(transitions) == 1
    status = fixture_env.client.get(f"/claims/{claim.id}", headers=_h())
    assert status.json()["status"] == "IN_APPROVAL"

    # The signed artifact is immutable and reachable only through its event id.
    artifact = fixture_env.client.get(
        f"/claims/{claim.id}/approval-pack/artifacts/{signed[0]['id']}", headers=_h()
    )
    assert artifact.status_code == 200
    assert artifact.headers["content-type"].startswith("application/pdf")
    assert artifact.headers["x-content-type-options"] == "nosniff"
    assert artifact.headers["cache-control"] == "private, no-store"
    assert artifact.headers["etag"].strip('"') == signed[0]["payload"]["artifact_sha256"]
    assert hashlib.sha256(artifact.content).hexdigest() == (
        signed[0]["payload"]["artifact_sha256"]
    )


def test_route_review_and_route_index_commit_together_after_a_failure(
    fixture_env, monkeypatch,
):
    claim = _seed(fixture_env, suffix="ROUTE")
    _sign(fixture_env, claim)
    resolved = _events(fixture_env.app, "review.resolved", claim.id)[0]
    event = SimpleNamespace(
        type="review.resolved",
        payload=resolved["payload"],
        actor=resolved["actor"],
        claim_id=claim.id,
        id=resolved["id"],
    )
    service = fixture_env.app.state.approval_pack_agent
    original_record = service._record
    failed = False

    def fail_before_route_index(session, **kwargs):
        nonlocal failed
        if kwargs["event_type"] == "pack.routed" and not failed:
            failed = True
            raise RuntimeError("injected route-index failure")
        return original_record(session, **kwargs)

    monkeypatch.setattr(service, "_record", fail_before_route_index)
    with pytest.raises(RuntimeError, match="route-index"):
        service.consume(event)
    approval_reviews = [
        row
        for row in _events(fixture_env.app, "review.created", claim.id)
        if row["payload"].get("subtype") == "approval_pack"
    ]
    assert approval_reviews == []
    assert _events(fixture_env.app, "pack.routed", claim.id) == []

    monkeypatch.setattr(service, "_record", original_record)
    service.consume(event)
    service.consume(event)
    approval_reviews = [
        row
        for row in _events(fixture_env.app, "review.created", claim.id)
        if row["payload"].get("subtype") == "approval_pack"
    ]
    assert len(approval_reviews) == 1
    assert len(_events(fixture_env.app, "pack.routed", claim.id)) == 1


def test_note_reject_signs_nothing_and_keeps_the_claim_pack_ready(fixture_env):
    claim = _seed(fixture_env, suffix="J")
    rejected = _resolve(
        fixture_env,
        claim.review_id,
        "reject",
        "NOTE_REVIEW@2",
        {
            **_sign_payload(claim.draft_id, claim.body_sha256),
            "reason": "The savings narrative does not match the ledger.",
        },
    )
    assert rejected.status_code == 200, rejected.text
    _drain(fixture_env.app)

    assert _events(fixture_env.app, "pack.note_sign_prepared", claim.id) == []
    assert _events(fixture_env.app, "pack.note_signed", claim.id) == []
    assert _reviews(fixture_env.app, claim.id, "PACK_REVIEW") == []
    rejections = _events(fixture_env.app, "pack.note_review_rejected", claim.id)
    assert len(rejections) == 1
    drafts = _drafts(fixture_env.app, claim.id)
    # The version is retained and returned to `draft`; nothing regenerates.
    assert [(row["version"], row["status"]) for row in drafts] == [(1, "draft")]
    status = fixture_env.client.get(f"/claims/{claim.id}", headers=_h())
    assert status.json()["status"] == "PACK_READY"


# --- 5. authority-band boundaries and the T-03 gate ---------------------------------


def test_exactly_four_million_routes_md_and_one_cent_more_blocks_on_t03(tmp_path):
    at_bound = _build(tmp_path, "band-md", pack=_fixture_pack(tmp_path, t03_live=False))
    claim = _seed(at_bound, suffix="M", reserve_total=FOUR_MILLION)
    _sign(at_bound, claim)
    _drain(at_bound.app)
    packs = _reviews(at_bound.app, claim.id, "PACK_REVIEW")
    # Inclusive upper bound: exactly KES 4,000,000.00 is still the MD's band.
    assert len(packs) == 1
    assert packs[0]["payload"]["required_role"] == "md"
    assert packs[0]["payload"]["routing_amount_cents"] == FOUR_MILLION
    assert _events(at_bound.app, "template.rendered", claim.id)
    assert not [
        event
        for event in _events(at_bound.app, "template.rendered", claim.id)
        if event["payload"].get("template_id") == "T-03"
    ]

    over = _build(tmp_path, "band-over", pack=_fixture_pack(tmp_path, t03_live=False))
    over_claim = _seed(over, suffix="O", reserve_total=FOUR_MILLION + 1)
    workspace = _workspace(over, over_claim)
    assert workspace["signable"] is True
    blocked = _resolve(
        over,
        over_claim.review_id,
        "approve",
        "NOTE_REVIEW@2",
        _sign_payload(
            workspace["current_draft"]["id"], workspace["current_draft"]["body_sha256"]
        ),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "ROUTING_BLOCKED_ON_INPUTS"
    assert blocked.json()["blocked_on"] == "open-item-6"
    _drain(over.app)
    # No signed version, PACK_REVIEW, notification, or FSM hop was created.
    assert _events(over.app, "pack.note_sign_prepared", over_claim.id) == []
    assert _events(over.app, "pack.note_signed", over_claim.id) == []
    assert _events(over.app, "pack.routed", over_claim.id) == []
    assert _reviews(over.app, over_claim.id, "PACK_REVIEW") == []
    assert over.client.get(
        f"/claims/{over_claim.id}", headers=_h()
    ).json()["status"] == "PACK_READY"


def test_one_cent_over_four_million_routes_chairman_when_t03_is_live(tmp_path):
    env = _build(tmp_path, "band-chair", pack=_fixture_pack(tmp_path, t03_live=True))
    claim = _seed(env, suffix="C", reserve_total=FOUR_MILLION + 1)
    _sign(env, claim)
    _drain(env.app)

    packs = _reviews(env.app, claim.id, "PACK_REVIEW")
    assert len(packs) == 1
    # PRD-02 precedes PRD-08: the chairman owns approval above KES 4M.
    assert packs[0]["payload"]["required_role"] == "chairman"
    assert packs[0]["payload"]["routing_amount_cents"] == FOUR_MILLION + 1
    alerts = [
        event
        for event in _events(env.app, "template.rendered", claim.id)
        if event["payload"].get("template_id") == "T-03"
    ]
    # R-12's alert is rendered before the approval item exists.
    assert len(alerts) == 1
    routed = _events(env.app, "pack.routed", claim.id)
    assert [row["template_id"] for row in routed[0]["payload"]["side_effects"]] == ["T-03"]


def test_the_pack_authority_matrix_and_r12_remain_the_binding_contract():
    matrix = yaml.safe_load(
        (MOTOR_PACK / "routing" / "authority_matrix.yaml").read_text(encoding="utf-8")
    )
    assert [row["max"] for row in matrix] == [
        100_000_00, 700_000_00, 1_500_000_00, 4_000_000_00, None,
    ]
    assert matrix[-1]["role"] == "chairman"
    assert matrix[-1]["side_effects"] == ["render T-03"]
    assert matrix[3]["role"] == "md"

    rule = yaml.safe_load((MOTOR_PACK / "rules" / "R-12.yaml").read_text(encoding="utf-8"))
    assert rule["when"] == {">": [{"var": "amount"}, 4_000_000_00]}
    assert rule["outcome"]["draft_template"] == "T-03"

    registry = yaml.safe_load(
        (MOTOR_PACK / "templates" / "registry.yaml").read_text(encoding="utf-8")
    )
    t03 = next(row for row in registry["templates"] if row["id"] == "T-03")
    # The production alert body is still uncaptured; the builder invented none.
    assert t03["status"] == "pending_capture"
    assert t03["body_ref"] is None
    assert t03["required_fields"] == []


# --- 6. exact-role approval queue ----------------------------------------------------


def test_only_the_exact_required_role_sees_and_resolves_the_approval(fixture_env):
    claim = _seed(fixture_env, suffix="R")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    review_id = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]["id"]

    def band(actor: str) -> list[dict[str, Any]]:
        response = fixture_env.client.get(
            "/reviews?scope=band&status=open", headers=_h(actor)
        )
        assert response.status_code == 200, response.text
        return [
            item for item in response.json()["items"] if item["subtype"] == "approval_pack"
        ]

    assert [item["id"] for item in band(MANAGER)] == [review_id]
    # A wider band does not silently take another role's approval item.
    assert band(GM) == []
    assert band(CHAIRMAN) == []
    assert band(MD) == []
    pool = fixture_env.client.get(
        "/reviews?scope=pool&status=open", headers=_h(GM)
    )
    assert pool.status_code == 200
    assert review_id not in {row["id"] for row in pool.json()["items"]}
    hidden = fixture_env.client.get(f"/reviews/{review_id}", headers=_h(GM))
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "REVIEW_NOT_FOUND"
    # Even claim assignment cannot widen the immutable exact-role snapshot.
    with fixture_env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :actor WHERE id = :claim_id"),
            {"actor": GM, "claim_id": claim.id},
        )
    mine = fixture_env.client.get(
        "/reviews?scope=mine&status=open", headers=_h(GM)
    )
    assert mine.status_code == 200
    assert review_id not in {row["id"] for row in mine.json()["items"]}

    item = band(MANAGER)[0]
    payload = {
        "capability_id": "pack.route",
        "merged_event_id": item["payload"]["merged_event_id"],
        "note_signed_event_id": item["payload"]["note_signed_event_id"],
        "draft_id": item["payload"]["draft_id"],
        "body_sha256": item["payload"]["body_sha256"],
        "routing_amount_cents": item["payload"]["routing_amount_cents"],
        "required_role": item["payload"]["required_role"],
        "diff": {"typed_changes": [], "prose_change_ratio": 0},
    }
    denied = _resolve(fixture_env, review_id, "approve", "PACK_REVIEW@2", payload, actor=GM)
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_BAND"
    denials = [
        event
        for event in _events(fixture_env.app, "authz.denied", claim.id)
        if event["payload"]["review_id"] == review_id
    ]
    assert len(denials) == 1
    assert denials[0]["payload"]["code"] == "FORBIDDEN_BAND"
    assert _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]["status"] == "open"


def test_a_changed_routing_input_keeps_the_item_open_as_route_stale(fixture_env):
    claim = _seed(fixture_env, suffix="Z")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    item = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]
    payload = {
        "capability_id": "pack.route",
        "merged_event_id": item["payload"]["merged_event_id"],
        "note_signed_event_id": item["payload"]["note_signed_event_id"],
        "draft_id": item["payload"]["draft_id"],
        "body_sha256": item["payload"]["body_sha256"],
        "routing_amount_cents": item["payload"]["routing_amount_cents"],
        "required_role": item["payload"]["required_role"],
        "diff": {"typed_changes": [], "prose_change_ratio": 0},
    }
    # A new committed reserve version changes the routed figure.
    fixture_env.client.patch(
        f"/claims/{claim.id}/fields",
        json={
            "writes": [
                {
                    "path": "reserve.total",
                    "value": RESERVE_TOTAL + 100,
                    "value_type": "money",
                    "source_type": "human",
                    "source_ref": {"user_id": OFFICER},
                    "verification_state": "human_verified",
                }
            ]
        },
        headers=_h(),
    )
    stale = _resolve(
        fixture_env, item["id"], "approve", "PACK_REVIEW@2", payload, actor=MANAGER
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "APPROVAL_ROUTE_STALE"
    assert stale.json()["current_amount_cents"] == RESERVE_TOTAL + 100
    assert _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]["status"] == "open"
    assert fixture_env.client.get(
        f"/claims/{claim.id}", headers=_h()
    ).json()["status"] == "IN_APPROVAL"


# --- 7. the S-3 approval loop --------------------------------------------------------


def _approval_payload(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "capability_id": "pack.route",
        "merged_event_id": item["payload"]["merged_event_id"],
        "note_signed_event_id": item["payload"]["note_signed_event_id"],
        "draft_id": item["payload"]["draft_id"],
        "body_sha256": item["payload"]["body_sha256"],
        "routing_amount_cents": item["payload"]["routing_amount_cents"],
        "required_role": item["payload"]["required_role"],
        "diff": {"typed_changes": [], "prose_change_ratio": 0},
        **extra,
    }


def test_manager_approve_reaches_approved_and_annotation_mutates_no_artifact(
    fixture_env,
):
    claim = _seed(fixture_env, suffix="A")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    item = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]
    signed_before = _events(fixture_env.app, "pack.note_signed", claim.id)[0]
    artifact_before = fixture_env.app.state.blob_store.get(
        signed_before["payload"]["artifact_blob_key"]
    )

    unannotated = _resolve(
        fixture_env, item["id"], "edit_approve", "PACK_REVIEW@2",
        _approval_payload(item), actor=MANAGER,
    )
    assert unannotated.status_code == 422
    assert "annotation" in unannotated.json()["detail"]

    annotated = _resolve(
        fixture_env, item["id"], "edit_approve", "PACK_REVIEW@2",
        _approval_payload(item, annotation="Approved; confirm the garage on release."),
        actor=MANAGER,
    )
    assert annotated.status_code == 200, annotated.text
    _drain(fixture_env.app)

    assert fixture_env.client.get(
        f"/claims/{claim.id}", headers=_h()
    ).json()["status"] == "APPROVED"
    signed_after = _events(fixture_env.app, "pack.note_signed", claim.id)
    assert len(signed_after) == 1
    assert fixture_env.app.state.blob_store.get(
        signed_before["payload"]["artifact_blob_key"]
    ) == artifact_before
    drafts = _drafts(fixture_env.app, claim.id)
    assert [row["status"] for row in drafts] == ["signed"]
    # The annotation lives only in the resolution event.
    resolved = [
        event
        for event in _events(fixture_env.app, "review.resolved", claim.id)
        if event["payload"].get("review_id") == item["id"]
    ]
    assert resolved[0]["payload"]["annotation"].startswith("Approved;")


def test_manager_rejection_returns_pack_ready_and_captures_one_correction_case(
    fixture_env,
):
    claim = _seed(fixture_env, suffix="X")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    item = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]
    signed_draft_id = item["payload"]["draft_id"]

    unstructured = _resolve(
        fixture_env, item["id"], "reject", "PACK_REVIEW@2",
        _approval_payload(item, reason="No."), actor=MANAGER,
    )
    assert unstructured.status_code == 422

    rejected = _resolve(
        fixture_env,
        item["id"],
        "reject",
        "PACK_REVIEW@2",
        _approval_payload(
            item,
            reason="The agreed quote is wrong.",
            reasons=[
                {
                    "code": "figure_mismatch",
                    "detail": "The agreed quote does not match the assessor report.",
                    "field_path": "assessment.agreed_quote",
                }
            ],
            diff={
                "typed_changes": [
                    {"path": "assessment.agreed_quote", "kind": "money"}
                ],
                "prose_change_ratio": 0,
            },
        ),
        actor=MANAGER,
    )
    assert rejected.status_code == 200, rejected.text
    _drain(fixture_env.app)

    assert fixture_env.client.get(
        f"/claims/{claim.id}", headers=_h()
    ).json()["status"] == "PACK_READY"
    drafts = {row["id"]: row for row in _drafts(fixture_env.app, claim.id)}
    # The signed version is retained exactly as signed.
    assert drafts[signed_draft_id]["status"] == "signed"
    assert drafts[signed_draft_id]["signed_by"] == OFFICER
    revisions = [
        row for row in drafts.values() if row["status"] == "in_review"
    ]
    assert len(revisions) == 1
    rejection = revisions[0]["body"]["manager_rejection"]
    assert rejection["rejected_by"] == MANAGER
    assert rejection["reasons"][0]["field_path"] == "assessment.agreed_quote"
    # Reasons are metadata only: no commentary paragraph was rewritten.
    sections = {
        section["template_slot"]: section for section in revisions[0]["body"]["sections"]
    }
    assert "agreed quote does not match" not in sections["savings_narrative"]["content"]

    notes = [
        row for row in _reviews(fixture_env.app, claim.id, "NOTE_REVIEW")
        if row["status"] == "open"
    ]
    assert len(notes) == 1
    assert notes[0]["payload"]["manager_rejection"]["review_id"] == item["id"]

    cases = _rows(
        fixture_env.app,
        "SELECT id, origin, expected, tags FROM test_cases WHERE origin = "
        "'production_correction'",
    )
    assert len(cases) == 1
    assert "_capture" not in cases[0]["expected"]


def test_manager_rejection_draft_and_review_are_atomic_on_replay(
    fixture_env, monkeypatch,
):
    claim = _seed(fixture_env, suffix="REJECT")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    item = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]
    rejected = _resolve(
        fixture_env,
        item["id"],
        "reject",
        "PACK_REVIEW@2",
        _approval_payload(
            item,
            reason="Correct the quote.",
            reasons=[
                {
                    "code": "figure_mismatch",
                    "detail": "The quote is incorrect.",
                    "field_path": "assessment.agreed_quote",
                }
            ],
            diff={
                "typed_changes": [
                    {"path": "assessment.agreed_quote", "kind": "money"}
                ],
                "prose_change_ratio": 0,
            },
        ),
        actor=MANAGER,
    )
    assert rejected.status_code == 200
    resolved = next(
        event
        for event in _events(fixture_env.app, "review.resolved", claim.id)
        if event["payload"].get("review_id") == item["id"]
    )
    event = SimpleNamespace(
        type="review.resolved",
        payload=resolved["payload"],
        actor=resolved["actor"],
        claim_id=claim.id,
        id=resolved["id"],
    )
    service = fixture_env.app.state.approval_pack_agent
    original_record = service._record
    failed = False

    def fail_before_revision_review(session, **kwargs):
        nonlocal failed
        if kwargs["event_type"] == "review.created" and not failed:
            failed = True
            raise RuntimeError("injected revision-review failure")
        return original_record(session, **kwargs)

    monkeypatch.setattr(service, "_record", fail_before_revision_review)
    with pytest.raises(RuntimeError, match="revision-review"):
        service.consume(event)
    assert [row["status"] for row in _drafts(fixture_env.app, claim.id)] == [
        "signed"
    ]
    assert [
        row
        for row in _events(fixture_env.app, "review.created", claim.id)
        if row["correlation_id"] == item["id"]
    ] == []

    monkeypatch.setattr(service, "_record", original_record)
    service.consume(event)
    service.consume(event)
    drafts = _drafts(fixture_env.app, claim.id)
    assert [row["status"] for row in drafts] == ["signed", "in_review"]
    revision_reviews = [
        row
        for row in _events(fixture_env.app, "review.created", claim.id)
        if row["correlation_id"] == item["id"]
    ]
    assert len(revision_reviews) == 1


def test_manager_rejection_without_a_field_path_stays_visibly_blocked(fixture_env):
    claim = _seed(fixture_env, suffix="B")
    _sign(fixture_env, claim)
    _drain(fixture_env.app)
    item = _reviews(fixture_env.app, claim.id, "PACK_REVIEW")[0]
    rejected = _resolve(
        fixture_env,
        item["id"],
        "reject",
        "PACK_REVIEW@2",
        _approval_payload(
            item,
            reason="The note reads badly.",
            reasons=[{"code": "narrative_unclear", "detail": "The narrative is unclear."}],
        ),
        actor=MANAGER,
    )
    assert rejected.status_code == 200, rejected.text
    _drain(fixture_env.app)
    cases = _rows(
        fixture_env.app,
        "SELECT id, origin, expected FROM test_cases WHERE origin = "
        "'production_correction'",
    )
    assert len(cases) == 1
    # The case is captured and visibly incomplete; nothing is fabricated.
    assert cases[0]["expected"]["_capture"]["status"] == "blocked_on_inputs"
    assert "corrected_path" in cases[0]["expected"]["_capture"]["missing_inputs"]


# --- 8. artifact isolation -----------------------------------------------------------


def test_cross_claim_artifact_fetch_is_404_and_raw_blob_keys_are_never_accepted(
    fixture_env,
):
    first = _seed(fixture_env, suffix="P1")
    second = _seed(fixture_env, suffix="P2")
    merged = _events(fixture_env.app, "pack.merged", first.id)[0]

    ok = fixture_env.client.get(
        f"/claims/{first.id}/approval-pack/artifacts/{merged['id']}", headers=_h()
    )
    assert ok.status_code == 200
    assert ok.headers["etag"].strip('"') == merged["payload"]["sha256"]
    accesses = _events(fixture_env.app, "pack.artifact_accessed", first.id)
    assert len(accesses) == 1
    assert accesses[0]["payload"] == {
        "artifact_event_id": merged["id"],
        "artifact_event_type": "pack.merged",
        "artifact_sha256": merged["payload"]["sha256"],
        "actor": OFFICER,
    }

    crossed = fixture_env.client.get(
        f"/claims/{second.id}/approval-pack/artifacts/{merged['id']}", headers=_h()
    )
    assert crossed.status_code == 404
    assert crossed.json()["code"] == "ARTIFACT_NOT_FOUND"

    # A raw blob key is not an event id and resolves to nothing.
    raw = fixture_env.client.get(
        f"/claims/{first.id}/approval-pack/artifacts/"
        f"{merged['payload']['blob_key'].replace('/', '%2F')}",
        headers=_h(),
    )
    assert raw.status_code == 404

    # A non-allowlisted event on the same claim is not an artifact index.
    drafted = _events(fixture_env.app, "pack.note_drafted", first.id)[0]
    refused = fixture_env.client.get(
        f"/claims/{first.id}/approval-pack/artifacts/{drafted['id']}", headers=_h()
    )
    assert refused.status_code == 404

    denied = fixture_env.client.get(
        f"/claims/{first.id}/approval-pack/artifacts/{merged['id']}",
        headers=_h(OUTSIDER),
    )
    assert denied.status_code == 403
    # Refused, cross-claim, raw-key, and non-artifact attempts create no access
    # record; only bytes actually returned to an authorised actor are logged.
    assert len(_events(fixture_env.app, "pack.artifact_accessed", first.id)) == 1


# --- 9. the ICON seam stays a slot ---------------------------------------------------


def test_icon_note_entry_remains_pending_capture_with_no_field_order(fixture_env):
    icon = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "icon.yaml").read_text(encoding="utf-8")
    )
    field_set = icon["field_sets"]["icon.note_entry"]
    assert field_set == {
        "status": "pending_capture",
        "blocked_on": "open-item-3",
        "fields": [],
    }
    claim = _seed(fixture_env, suffix="K")
    exposed = _workspace(fixture_env, claim)["icon_note_entry"]
    assert exposed["status"] == "pending_capture"
    assert exposed["blocked_on"] == "open-item-3"
    assert exposed["fields"] == []

    # No PRD-09 adapter, click path, projection operation, or payment op exists.
    sources = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((REPO / "agents" / "approval_pack_agent").glob("*.py"))
    )
    for forbidden in (
        "funds_transfer", "transfer_funds", "execute_payment", "paste_assist",
        "click_path", "readback_check", "adapter.execute", "graph_client",
    ):
        assert forbidden not in sources


# --- constitution pins ---------------------------------------------------------------


def test_signing_is_not_an_autonomy_capability_and_ceilings_are_unchanged():
    policies = yaml.safe_load(
        (MOTOR_PACK / "autonomy" / "policies.yaml").read_text(encoding="utf-8")
    )
    by_id = {row["id"]: row for row in policies["capabilities"]}
    assert by_id["pack.note_draft"]["max_level"] == "L3"
    assert by_id["pack.merge"]["max_level"] == "L4"
    assert by_id["pack.route"]["max_level"] == "L4"
    ids = set(by_id)
    assert not any(value.startswith(("approval.", "sign.")) for value in ids)
    assert "salvage.award" not in ids

    contracts = yaml.safe_load(
        (MOTOR_PACK / "review" / "contracts.yaml").read_text(encoding="utf-8")
    )
    # The review-item type enum stays closed at exactly 17 types.
    assert len(contracts["types"]) == 17
    note = contracts["types"]["NOTE_REVIEW"]["subtypes"]["approval_note"]
    approval = contracts["types"]["PACK_REVIEW"]["subtypes"]["approval_pack"]
    assert note["resolution_schema"] == "NOTE_REVIEW@2"
    assert approval["resolution_schema"] == "PACK_REVIEW@2"
    # NOTE_REVIEW@1 is retained for historical replay.
    assert contracts["types"]["NOTE_REVIEW"]["resolution_schema"] == "NOTE_REVIEW@1"
    assert approval["band_amount_path"] is None
    assert approval["band_role_path"] == "required_role"
    assert contracts["types"]["PACK_REVIEW"]["band_amount_path"] == (
        "assessment.agreed_quote"
    )


def test_every_new_pack_event_is_mapped_through_the_single_ledger_writer(fixture_env):
    from claim_core.ledger import ACTION_MAP

    for event_type in (
        "pack.note_autosaved",
        "pack.note_review_rejected",
        "pack.note_sign_prepared",
        "pack.note_signed",
        "pack.routed",
        "pack.artifact_accessed",
    ):
        assert ACTION_MAP[event_type] == event_type

    claim = _seed(fixture_env, suffix="D")
    _autosave(
        fixture_env,
        claim,
        base_draft_id=claim.draft_id,
        base_body_sha256=claim.body_sha256,
        key="ledger",
    )
    _sign(fixture_env, claim)
    merged = _events(fixture_env.app, "pack.merged", claim.id)[0]
    artifact = fixture_env.client.get(
        f"/claims/{claim.id}/approval-pack/artifacts/{merged['id']}",
        headers=_h(),
    )
    assert artifact.status_code == 200
    _drain(fixture_env.app)
    actions = {
        row["action"]
        for row in _rows(
            fixture_env.app,
            "SELECT action FROM audit_ledger WHERE claim_id = :claim_id",
            claim_id=claim.id,
        )
    }
    assert {
        "pack.note_autosaved",
        "pack.note_sign_prepared",
        "pack.note_signed",
        "pack.routed",
        "pack.artifact_accessed",
    } <= actions
    detail = json.dumps(
        _rows(
            fixture_env.app,
            "SELECT detail FROM audit_ledger WHERE claim_id = :claim_id",
            claim_id=claim.id,
        )
    )
    # No commentary text, manager free text, or decrypted PII enters the ledger.
    assert "junction collision" not in detail
    assert "broker@example.co.ke" not in detail
