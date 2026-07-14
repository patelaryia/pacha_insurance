"""PACKET-10 acceptance — PRD-04 §4.2/§4.3/§4.5 review-queue substrate.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-10_review_queue.md §3. Scenario 4 (403 + ledger) and
scenario 6 (reject → production_correction case) are implemented verbatim.
No browser, broker, network, or live model call is permitted.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"
REVIEW_PACK = MOTOR_PACK / "review"

AGENT = "agent:intake"
OFFICER = "user:01HREVQOFFICER00000000AAAA"
OFFICER_2 = "user:01HREVQOFFICER00000000BBBB"
ACM = "user:01HREVQASSTMANAGER0000AAAA"
CM = "user:01HREVQCLAIMSMANAGER00AAAA"
AUDITOR = "user:01HREVQAUDITOR00000000AAAA"
GHOST = "user:01HREVQUNMAPPED0000000AAAA"

ROLES = {
    OFFICER: "claims_officer",
    OFFICER_2: "claims_officer",
    ACM: "asst_claims_manager",
    CM: "claims_manager",
    AUDITOR: "auditor",
}

SEVENTEEN = {
    "FIELD_VERIFY", "DOC_CLASSIFY", "DOC_SPLIT", "CONSISTENCY_FLAG",
    "DRAFT_RELEASE", "MODE_CONFIRM", "NOTE_REVIEW", "PACK_REVIEW",
    "EX_GRATIA", "EXCEPTION", "PROMOTION_SIGNOFF", "SAMPLE_REVIEW",
    "PASTE_READBACK_CHECK", "PROCEED_PARTIAL", "KYC_VERIFY", "EFT_MATCH",
    "REOPEN_PROMPT",
}


def _h(actor: str) -> dict:
    return {"X-Actor": actor}


def _build(tmp_path, name: str):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    queue = build_review_queue(app, roles=dict(ROLES))
    return TestClient(app), app, queue


@pytest.fixture()
def env(tmp_path):
    return _build(tmp_path, "pacha_acc10")


def _claim(client) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, claim_id: str, label: str = "source") -> str:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(image).text((40, 100), label, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    response = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (f"{label}.png", io.BytesIO(output.getvalue()), "image/png")},
        data={"source_channel": "test", "source_ref": f"msg-{label}"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _assign(app, claim_id: str, officer: str) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :officer WHERE id = :claim_id"),
            {"officer": officer, "claim_id": claim_id},
        )


def _emit(app, event_type: str, payload: dict, claim_id: str | None = None) -> str:
    with Session(app.state.engine) as session:
        event = app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=AGENT,
            correlation_id=None,
        )
        session.commit()
        return event.id


def _drain(app, cycles: int = 16) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _review(app, claim_id: str | None, payload: dict) -> str:
    return _emit(app, "review.created", payload, claim_id)


def _rows(app) -> list[dict]:
    with app.state.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, claim_id, type, subtype, status, assigned_to, payload, "
                "resolved_by, resolution, resolution_schema_version "
                "FROM review_items ORDER BY created_at, id"
            )
        ).mappings()
        output = []
        for row in rows:
            item = dict(row)
            if isinstance(item["payload"], str):
                item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output


def _item_id(app, source_event_id: str) -> str:
    with app.state.engine.connect() as connection:
        item_id = connection.execute(
            text("SELECT id FROM review_items WHERE source_event_id = :event_id"),
            {"event_id": source_event_id},
        ).scalar()
    assert item_id is not None, "review item was not projected"
    return str(item_id)


def _ledger_actions(app) -> list[str]:
    with app.state.engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT action FROM audit_ledger ORDER BY seq")
            )
        ]


def _events(app, event_type: str) -> list[dict]:
    with app.state.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, claim_id, actor, payload FROM events "
                "WHERE type = :event_type ORDER BY seq"
            ),
            {"event_type": event_type},
        ).mappings()
        output = []
        for row in rows:
            item = dict(row)
            if isinstance(item["payload"], str):
                item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output


def _diff(typed_changes: list[dict] | None = None, ratio: float = 0.0) -> dict:
    return {"typed_changes": typed_changes or [], "prose_change_ratio": ratio}


def _resolve(
    client,
    item_id: str,
    actor: str,
    *,
    action: str,
    payload: dict,
    schema_version: str,
):
    return client.post(
        f"/reviews/{item_id}/resolve",
        json={
            "action": action,
            "schema_version": schema_version,
            "payload": payload,
        },
        headers=_h(actor),
    )


def _note_item(app, claim_id: str) -> str:
    event_id = _review(
        app,
        claim_id,
        {
            "type": "NOTE_REVIEW",
            "capability_id": "pack.note_draft",
            "output": {"note": "draft"},
            "citations": [{"document_id": "d", "page": 1}],
        },
    )
    _drain(app)
    return _item_id(app, event_id)


def _write_field(client, claim_id: str, *, path: str, value, value_type: str,
                 actor: str, source_type: str, verification_state: str,
                 source_ref: dict | None = None) -> object:
    write = {
        "path": path,
        "value": value,
        "value_type": value_type,
        "source_type": source_type,
        "verification_state": verification_state,
    }
    if source_ref is not None:
        write["source_ref"] = source_ref
        write["confidence"] = 0.99
    return client.patch(
        f"/claims/{claim_id}/fields", json={"writes": [write]}, headers=_h(actor)
    )


def _human_money(client, claim_id: str, path: str, cents: int) -> None:
    response = _write_field(
        client,
        claim_id,
        path=path,
        value=cents,
        value_type="money",
        actor=CM,
        source_type="human",
        verification_state="human_verified",
    )
    assert response.status_code == 200, response.text


def _current_field(app, claim_id: str, path: str) -> dict:
    with app.state.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT value, value_type, source_type, verification_state "
                "FROM claim_fields WHERE claim_id = :claim_id AND path = :path "
                "AND superseded_by IS NULL"
            ),
            {"claim_id": claim_id, "path": path},
        ).mappings().first()
    assert row is not None, f"no current field at {path}"
    item = dict(row)
    if isinstance(item["value"], str):
        try:
            item["value"] = json.loads(item["value"])
        except json.JSONDecodeError:
            pass
    return item


# --- projection + closed enum -------------------------------------------------------


def test_review_created_projects_one_idempotent_row(env):
    client, app, _queue = env
    claim_id = _claim(client)
    event_id = _review(
        app,
        claim_id,
        {
            "type": "NOTE_REVIEW",
            "capability_id": "pack.note_draft",
            "output": {"note": "draft text"},
            "citations": [{"document_id": "doc", "page": 1}],
        },
    )
    _drain(app)
    _drain(app)
    rows = [row for row in _rows(app) if row["claim_id"] == claim_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "NOTE_REVIEW"
    assert row["status"] == "open"
    assert row["payload"]["citations"] == [{"document_id": "doc", "page": 1}]
    assert _item_id(app, event_id) == row["id"]


def test_backfill_replays_history_idempotently(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc10_bf.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    client = TestClient(app)
    claim_id = _claim(client)
    _review(app, claim_id, {"type": "SAMPLE_REVIEW", "capability_id": "pack.merge"})
    _review(app, claim_id, {"type": "EXCEPTION", "subtype": "grader_critical_fail"})

    queue = build_review_queue(app, roles=dict(ROLES))
    queue.backfill(actor="system")
    assert len(_rows(app)) == 2
    queue.backfill(actor="system")
    _drain(app)
    assert len(_rows(app)) == 2


def test_unknown_type_projects_as_exception_subtype_never_dropped(env):
    _client, app, _queue = env
    event_id = _emit(
        app,
        "review.created",
        {"type": "BUDGET_EXCEEDED", "note": "not a real type"},
        None,
    )
    _drain(app)
    row = next(r for r in _rows(app) if r["id"] == _item_id(app, event_id))
    assert row["type"] == "EXCEPTION"
    assert row["subtype"] == "unknown_review_type"
    assert row["payload"]["type"] == "BUDGET_EXCEEDED"


def test_contract_registry_ships_seventeen_four_part_contracts():
    contracts = yaml.safe_load(
        (REVIEW_PACK / "contracts.yaml").read_text(encoding="utf-8")
    )
    types = contracts["types"] if "types" in contracts else contracts
    assert set(types) == SEVENTEEN
    for type_name, contract in types.items():
        assert contract["producing_events"], type_name
        assert isinstance(contract["workspace_layout"], str), type_name
        assert contract["resolution_actions"] == [
            "approve",
            "edit_approve",
            "reject",
        ], type_name
        assert contract["resolution_schema"] == f"{type_name}@1", type_name
        assert contract["authorised_roles"], type_name
        schema_path = REVIEW_PACK / "schemas" / f"{type_name}@1.json"
        assert schema_path.exists(), type_name
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = set(schema.get("required", []))
        assert {"capability_id", "diff"} <= required, type_name
    for banded in ("PACK_REVIEW", "EX_GRATIA"):
        assert types[banded]["band_amount_path"] == "assessment.agreed_quote"


def test_eighteenth_type_fails_contract_load(tmp_path):
    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    review_dir = tmp_path / "review"
    shutil.copytree(REVIEW_PACK, review_dir)
    contracts_file = review_dir / "contracts.yaml"
    contracts = yaml.safe_load(contracts_file.read_text(encoding="utf-8"))
    types = contracts["types"] if "types" in contracts else contracts
    types["BUDGET_EXCEEDED"] = dict(types["EXCEPTION"])
    contracts_file.write_text(yaml.safe_dump(contracts), encoding="utf-8")

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc10_18.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    with pytest.raises(ValueError):
        build_review_queue(app, roles=dict(ROLES), contracts_path=review_dir)


# --- resolution mechanics -----------------------------------------------------------


def test_resolve_writes_row_event_actual_actor_and_ledger(env):
    client, app, _queue = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    item_id = _note_item(app, claim_id)

    response = _resolve(
        client,
        item_id,
        OFFICER_2,
        action="approve",
        payload={"capability_id": "pack.note_draft", "diff": _diff()},
        schema_version="NOTE_REVIEW@1",
    )
    assert response.status_code == 200, response.text

    row = next(r for r in _rows(app) if r["id"] == item_id)
    assert row["status"] == "resolved"
    assert row["resolved_by"] == OFFICER_2
    assert row["resolution"] == "approved"
    assert row["resolution_schema_version"] == "NOTE_REVIEW@1"

    resolved = _events(app, "review.resolved")
    assert len(resolved) == 1
    event = resolved[0]
    assert event["actor"] == OFFICER_2
    assert event["payload"]["review_id"] == item_id
    assert event["payload"]["type"] == "NOTE_REVIEW"
    assert event["payload"]["schema_version"] == "NOTE_REVIEW@1"
    assert event["payload"]["resolution"] == "approved"
    assert event["payload"]["capability_id"] == "pack.note_draft"

    _drain(app)
    assert "review.resolved" in _ledger_actions(app)


def test_double_resolve_409(env):
    client, app, _queue = env
    claim_id = _claim(client)
    item_id = _note_item(app, claim_id)
    payload = {"capability_id": "pack.note_draft", "diff": _diff()}
    first = _resolve(
        client, item_id, OFFICER, action="approve", payload=payload,
        schema_version="NOTE_REVIEW@1",
    )
    assert first.status_code == 200, first.text
    second = _resolve(
        client, item_id, CM, action="approve", payload=payload,
        schema_version="NOTE_REVIEW@1",
    )
    assert second.status_code == 409
    assert second.json()["code"] == "ALREADY_RESOLVED"


def test_schema_version_and_payload_are_validated(env):
    client, app, _queue = env
    claim_id = _claim(client)
    item_id = _note_item(app, claim_id)

    unknown = _resolve(
        client,
        item_id,
        OFFICER,
        action="approve",
        payload={"capability_id": "pack.note_draft", "diff": _diff()},
        schema_version="NOTE_REVIEW@9",
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "SCHEMA_VERSION_UNKNOWN"

    invalid = _resolve(
        client,
        item_id,
        OFFICER,
        action="approve",
        payload={"diff": _diff()},
        schema_version="NOTE_REVIEW@1",
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "PAYLOAD_INVALID"

    row = next(r for r in _rows(app) if r["id"] == item_id)
    assert row["status"] == "open"


def test_reject_requires_reason_free_text(env):
    client, app, _queue = env
    claim_id = _claim(client)
    item_id = _note_item(app, claim_id)

    missing = _resolve(
        client,
        item_id,
        OFFICER,
        action="reject",
        payload={"capability_id": "pack.note_draft", "diff": _diff()},
        schema_version="NOTE_REVIEW@1",
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "PAYLOAD_INVALID"

    accepted = _resolve(
        client,
        item_id,
        OFFICER,
        action="reject",
        payload={
            "capability_id": "pack.note_draft",
            "diff": _diff(),
            "reason": "figures do not match the assessor report",
        },
        schema_version="NOTE_REVIEW@1",
    )
    assert accepted.status_code == 200, accepted.text
    event = _events(app, "review.resolved")[-1]
    assert event["payload"]["resolution"] == "rejected"
    assert event["payload"]["reason"].startswith("figures do not match")


def test_agents_and_unmapped_users_cannot_resolve(env):
    client, app, _queue = env
    claim_id = _claim(client)
    item_id = _note_item(app, claim_id)
    payload = {"capability_id": "pack.note_draft", "diff": _diff()}

    for actor in (AGENT, "system", GHOST, AUDITOR):
        response = _resolve(
            client, item_id, actor, action="approve", payload=payload,
            schema_version="NOTE_REVIEW@1",
        )
        assert response.status_code == 403, actor
        assert response.json()["code"] == "FORBIDDEN_ROLE", actor

    row = next(r for r in _rows(app) if r["id"] == item_id)
    assert row["status"] == "open"
    _drain(app)
    assert _ledger_actions(app).count("authz.denied") >= 4


# --- scenario 4: bands --------------------------------------------------------------


def _band_item(client, app, cents: int | None) -> tuple[str, str]:
    claim_id = _claim(client)
    if cents is not None:
        _human_money(client, claim_id, "assessment.agreed_quote", cents)
    event_id = _review(
        app,
        claim_id,
        {"type": "PACK_REVIEW", "capability_id": "pack.merge"},
    )
    _drain(app)
    return claim_id, _item_id(app, event_id)


def test_band_boundary_is_inclusive_at_exactly_100_000_00(env):
    client, app, _queue = env
    _claim_id, item_id = _band_item(client, app, 100_000_00)
    response = _resolve(
        client,
        item_id,
        ACM,
        action="approve",
        payload={"capability_id": "pack.merge", "diff": _diff()},
        schema_version="PACK_REVIEW@1",
    )
    assert response.status_code == 200, response.text


def test_approval_outside_band_403_plus_ledger_entry(env):
    client, app, _queue = env
    _claim_id, item_id = _band_item(client, app, 100_000_01)
    payload = {"capability_id": "pack.merge", "diff": _diff()}

    denied = _resolve(
        client, item_id, ACM, action="approve", payload=payload,
        schema_version="PACK_REVIEW@1",
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_BAND"
    assert next(r for r in _rows(app) if r["id"] == item_id)["status"] == "open"

    _drain(app)
    assert "authz.denied" in _ledger_actions(app)

    approved = _resolve(
        client, item_id, CM, action="approve", payload=payload,
        schema_version="PACK_REVIEW@1",
    )
    assert approved.status_code == 200, approved.text


def test_officer_role_cannot_touch_band_gated_type(env):
    client, app, _queue = env
    _claim_id, item_id = _band_item(client, app, 1_000_00)
    response = _resolve(
        client,
        item_id,
        OFFICER,
        action="approve",
        payload={"capability_id": "pack.merge", "diff": _diff()},
        schema_version="PACK_REVIEW@1",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_ROLE"


def test_missing_band_amount_blocks_never_guesses(env):
    client, app, _queue = env
    _claim_id, item_id = _band_item(client, app, None)
    response = _resolve(
        client,
        item_id,
        CM,
        action="approve",
        payload={"capability_id": "pack.merge", "diff": _diff()},
        schema_version="PACK_REVIEW@1",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    assert next(r for r in _rows(app) if r["id"] == item_id)["status"] == "open"


# --- typed side effects -------------------------------------------------------------


def test_field_verify_resolution_is_append_only_human_write(env):
    client, app, _queue = env
    claim_id = _claim(client)
    document_id = _upload(client, claim_id)
    seeded = _write_field(
        client,
        claim_id,
        path="vehicle.reg",
        value="KAA 111A",
        value_type="string",
        actor=AGENT,
        source_type="extraction",
        verification_state="extracted",
        source_ref={
            "document_id": document_id,
            "page": 1,
            "bbox": [0.1, 0.1, 0.9, 0.9],
            "anchor_text": "KAA 111A",
        },
    )
    assert seeded.status_code == 200, seeded.text

    event_id = _review(
        app,
        claim_id,
        {
            "type": "FIELD_VERIFY",
            "capability_id": "doc.extract",
            "path": "vehicle.reg",
        },
    )
    _drain(app)
    response = _resolve(
        client,
        _item_id(app, event_id),
        OFFICER,
        action="edit_approve",
        payload={
            "capability_id": "doc.extract",
            "diff": _diff([{"path": "vehicle.reg", "kind": "text"}]),
            "corrected_fields": {"vehicle.reg": "KBB 222B"},
        },
        schema_version="FIELD_VERIFY@1",
    )
    assert response.status_code == 200, response.text

    current = _current_field(app, claim_id, "vehicle.reg")
    assert current["value"] == "KBB 222B"
    assert current["verification_state"] == "human_verified"
    assert current["source_type"] == "human"

    with app.state.engine.connect() as connection:
        versions = connection.execute(
            text(
                "SELECT COUNT(*) FROM claim_fields "
                "WHERE claim_id = :claim_id AND path = 'vehicle.reg'"
            ),
            {"claim_id": claim_id},
        ).scalar()
    assert versions == 2  # append-only: superseded + current

    overwrite = _write_field(
        client,
        claim_id,
        path="vehicle.reg",
        value="KCC 333C",
        value_type="string",
        actor=AGENT,
        source_type="extraction",
        verification_state="extracted",
        source_ref={
            "document_id": document_id,
            "page": 1,
            "bbox": [0.1, 0.1, 0.9, 0.9],
            "anchor_text": "KCC 333C",
        },
    )
    assert overwrite.status_code == 409
    assert overwrite.json()["code"] == "HUMAN_OVERRIDE_PROTECTED"


def _pending_decline(client, app) -> tuple[str, str]:
    claim_id = _claim(client)
    for state in ("TRIAGED", "AWAITING_DOCS"):
        moved = client.post(
            f"/claims/{claim_id}/transition", json={"to": state}, headers=_h(AGENT)
        )
        assert moved.status_code == 200, moved.text
    declined = client.post(
        f"/claims/{claim_id}/decline", json={"reason": "fraud"}, headers=_h(OFFICER)
    )
    assert declined.status_code == 202
    assert declined.json()["code"] == "APPROVAL_REQUIRED"
    _drain(app)
    row = next(
        r
        for r in _rows(app)
        if r["claim_id"] == claim_id and r["subtype"] == "decline_approval_required"
    )
    return claim_id, row["id"]


def _decline_payload(reason: str | None = None) -> dict:
    payload = {"capability_id": "triage.decline_draft", "diff": _diff()}
    if reason is not None:
        payload["reason"] = reason
    return payload


def test_decline_approval_requires_claims_manager_and_commits(env):
    client, app, _queue = env
    claim_id, item_id = _pending_decline(client, app)

    outside = _resolve(
        client, item_id, OFFICER, action="approve", payload=_decline_payload(),
        schema_version="EXCEPTION@1",
    )
    assert outside.status_code == 403
    assert outside.json()["code"] == "FORBIDDEN_ROLE"

    approved = _resolve(
        client, item_id, CM, action="approve", payload=_decline_payload(),
        schema_version="EXCEPTION@1",
    )
    assert approved.status_code == 200, approved.text

    status = client.get(f"/claims/{claim_id}", headers=_h(AGENT)).json()["status"]
    assert status == "DECLINED"
    transitions = [
        event
        for event in _events(app, "claim.status_changed")
        if event["claim_id"] == claim_id and event["payload"].get("to") == "DECLINED"
    ]
    assert transitions and transitions[-1]["payload"].get("approved_by_event")


def test_decline_rejection_leaves_claim_unchanged_and_unblocks(env):
    client, app, _queue = env
    claim_id, item_id = _pending_decline(client, app)

    rejected = _resolve(
        client,
        item_id,
        CM,
        action="reject",
        payload=_decline_payload("insufficient fraud evidence"),
        schema_version="EXCEPTION@1",
    )
    assert rejected.status_code == 200, rejected.text

    hydrated = client.get(f"/claims/{claim_id}", headers=_h(AGENT)).json()
    assert hydrated["status"] == "AWAITING_DOCS"
    assert not any(
        "claims_manager" in reason for reason in hydrated["blocked_reasons"]
    )


class SplitEngineStub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def apply_human_boundaries(self, document_id, *, boundaries, actor):
        self.calls.append(
            {"document_id": document_id, "boundaries": boundaries, "actor": actor}
        )
        return ["child-1", "child-2"]


def test_doc_split_resolution_calls_the_packet05_engine_method(env):
    client, app, _queue = env
    stub = SplitEngineStub()
    app.state.doc_intel = stub
    claim_id = _claim(client)
    event_id = _review(
        app,
        claim_id,
        {"type": "DOC_SPLIT", "document_id": "01HREVQDOCUMENT0000000AAAA"},
    )
    _drain(app)
    boundaries = [
        {"start_page": 1, "end_page": 2},
        {"start_page": 3, "end_page": 4},
    ]
    response = _resolve(
        client,
        _item_id(app, event_id),
        OFFICER,
        action="edit_approve",
        payload={
            "capability_id": "doc.split",
            "diff": _diff(),
            "boundaries": boundaries,
        },
        schema_version="DOC_SPLIT@1",
    )
    assert response.status_code == 200, response.text
    assert stub.calls == [
        {
            "document_id": "01HREVQDOCUMENT0000000AAAA",
            "boundaries": boundaries,
            "actor": OFFICER,
        }
    ]


def test_doc_split_without_engine_blocks_visibly(env):
    client, app, _queue = env
    assert not hasattr(app.state, "doc_intel")
    claim_id = _claim(client)
    event_id = _review(
        app,
        claim_id,
        {"type": "DOC_SPLIT", "document_id": "01HREVQDOCUMENT0000000AAAA"},
    )
    _drain(app)
    item_id = _item_id(app, event_id)
    response = _resolve(
        client,
        item_id,
        OFFICER,
        action="edit_approve",
        payload={
            "capability_id": "doc.split",
            "diff": _diff(),
            "boundaries": [
                {"start_page": 1, "end_page": 1},
                {"start_page": 2, "end_page": 2},
            ],
        },
        schema_version="DOC_SPLIT@1",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    assert next(r for r in _rows(app) if r["id"] == item_id)["status"] == "open"


def test_promotion_signoff_resolution_never_moves_a_level(env):
    client, app, _queue = env
    harness = app.state.eval_harness
    before = harness.autonomy.level("triage.ex_gratia")
    event_id = _review(
        app,
        None,
        {"type": "PROMOTION_SIGNOFF", "capability_id": "triage.ex_gratia"},
    )
    _drain(app)
    response = _resolve(
        client,
        _item_id(app, event_id),
        CM,
        action="approve",
        payload={"capability_id": "triage.ex_gratia", "diff": _diff()},
        schema_version="PROMOTION_SIGNOFF@1",
    )
    assert response.status_code == 200, response.text
    _drain(app)
    assert harness.autonomy.level("triage.ex_gratia") == before
    with app.state.engine.connect() as connection:
        changes = connection.execute(
            text("SELECT COUNT(*) FROM autonomy_changes")
        ).scalar()
    assert changes == 0


# --- queue reads --------------------------------------------------------------------


def test_queue_scopes_mine_and_pool(env):
    client, app, _queue = env
    mine = _claim(client)
    other = _claim(client)
    _assign(app, mine, OFFICER)
    _note_item(app, mine)
    _note_item(app, other)

    scoped = client.get("/reviews", params={"scope": "mine"}, headers=_h(OFFICER))
    assert scoped.status_code == 200, scoped.text
    assert {item["claim_id"] for item in scoped.json()["items"]} == {mine}
    assert all(item["assigned_to"] == OFFICER for item in scoped.json()["items"])

    pool = client.get("/reviews", params={"scope": "pool"}, headers=_h(OFFICER_2))
    assert pool.status_code == 200
    assert {item["claim_id"] for item in pool.json()["items"]} >= {mine, other}


def test_items_carry_the_claims_sla_clocks(env):
    client, app, _queue = env
    from claim_core.models import SlaClock
    from claim_core.service import new_ulid, utc_now

    claim_id = _claim(client)
    item_id = _note_item(app, claim_id)
    with Session(app.state.engine) as session:
        session.add(
            SlaClock(
                id=new_ulid(),
                claim_id=claim_id,
                definition_id="sla.acknowledge",
                started_at=utc_now(),
                state="running",
                started_by_event="01HREVQSLASTARTEVENT00AAAA",
            )
        )
        session.commit()

    response = client.get(
        "/reviews", params={"scope": "pool", "claim_id": claim_id}, headers=_h(OFFICER)
    )
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()["items"] if i["id"] == item_id)
    assert any(
        clock["definition_id"] == "sla.acknowledge" and clock["state"] == "running"
        for clock in item["sla"]
    )


# --- scenario 6: reject round-trips into a production_correction test case ----------


def test_scenario6_reject_roundtrips_into_production_correction_case(env):
    client, app, _queue = env
    claim_id = _claim(client)
    _human_money(client, claim_id, "assessment.agreed_quote", 4_500_00)
    item_id = _note_item(app, claim_id)

    response = _resolve(
        client,
        item_id,
        OFFICER,
        action="reject",
        payload={
            "capability_id": "pack.note_draft",
            "diff": _diff(
                [{"path": "assessment.agreed_quote", "kind": "money"}]
            ),
            "reason": "note quotes the wrong agreed figure",
        },
        schema_version="NOTE_REVIEW@1",
    )
    assert response.status_code == 200, response.text
    _drain(app)

    with app.state.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT origin, input_bundle, expected, tags FROM test_cases "
                "ORDER BY created_at, id"
            )
        ).mappings().all()
    cases = []
    for row in rows:
        case = dict(row)
        for key in ("input_bundle", "expected", "tags"):
            if isinstance(case[key], str):
                case[key] = json.loads(case[key])
        cases.append(case)
    corrections = [c for c in cases if c["origin"] == "production_correction"]
    assert len(corrections) == 1
    case = corrections[0]
    assert case["input_bundle"]["claim_id"] == claim_id
    assert "capability:pack.note_draft" in case["tags"]
    assert "failure_mode:rejected" in case["tags"]
    assert case["expected"]["fields"]["assessment.agreed_quote"] == 4_500_00
    assert case["expected"].get("_capture") is None  # complete, not blocked
