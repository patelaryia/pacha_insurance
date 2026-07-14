"""Focused PACKET-10 coverage for a provenance-valid FIELD_VERIFY edit."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from claim_core import ClaimCoreError
from claim_core.app import create_app
from cop_runtime import build_cop_runtime
from eval_harness import build_eval_harness
from review_queue import build_review_queue
from review_queue.rbac import load_authority_matrix, load_roles

REPO = Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"
AGENT = "agent:intake"
OFFICER = "user:01HREVQOFFICER00000000AAAA"
CM = "user:01HREVQCLAIMSMANAGER00AAAA"


def _build(tmp_path: Path, name: str) -> tuple[TestClient, object]:
    app = create_app(f"sqlite:///{tmp_path}/{name}.db")
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(
        app,
        roles={OFFICER: "claims_officer", CM: "claims_manager"},
    )
    return TestClient(app), app


def _drain(app: object) -> None:
    for _ in range(16):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _pending_decline(tmp_path: Path, name: str) -> tuple[TestClient, object, str, str]:
    client, app = _build(tmp_path, name)
    claim = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers={"X-Actor": AGENT},
    ).json()
    for state in ("TRIAGED", "AWAITING_DOCS"):
        response = client.post(
            f"/claims/{claim['id']}/transition",
            json={"to": state},
            headers={"X-Actor": AGENT},
        )
        assert response.status_code == 200
    response = client.post(
        f"/claims/{claim['id']}/decline",
        json={"reason": "fraud"},
        headers={"X-Actor": OFFICER},
    )
    assert response.status_code == 202
    _drain(app)
    with app.state.engine.connect() as connection:
        review_id = connection.execute(
            text(
                "SELECT id FROM review_items WHERE claim_id = :claim_id "
                "AND subtype = 'decline_approval_required'"
            ),
            {"claim_id": claim["id"]},
        ).scalar_one()
    return client, app, claim["id"], str(review_id)


def _approve_decline(client: TestClient, review_id: str):
    return client.post(
        f"/reviews/{review_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "EXCEPTION@1",
            "payload": {
                "capability_id": "triage.decline_draft",
                "diff": {"typed_changes": [], "prose_change_ratio": 0},
            },
        },
        headers={"X-Actor": CM},
    )


def test_field_verify_edits_through_append_only_human_write(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{tmp_path}/field_verify.db")
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles={OFFICER: "claims_officer"})
    client = TestClient(app)
    claim = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers={"X-Actor": AGENT},
    ).json()
    uploaded = client.post(
        f"/claims/{claim['id']}/documents",
        files={"file": ("registration.txt", b"KAA 111A", "text/plain")},
        data={"source_channel": "email", "source_ref": "message-1"},
        headers={"X-Actor": AGENT},
    )
    assert uploaded.status_code == 201
    document_id = uploaded.json()["id"]
    seeded = client.patch(
        f"/claims/{claim['id']}/fields",
        json={
            "writes": [
                {
                    "path": "vehicle.reg",
                    "value": "KAA 111A",
                    "value_type": "string",
                    "source_type": "extraction",
                    "verification_state": "extracted",
                    "confidence": 0.99,
                    "source_ref": {
                        "document_id": document_id,
                        "page": 1,
                        "bbox": [0.1, 0.1, 0.9, 0.9],
                        "anchor_text": "KAA 111A",
                    },
                }
            ]
        },
        headers={"X-Actor": AGENT},
    )
    assert seeded.status_code == 200
    with Session(app.state.engine) as session:
        event = app.state.record_event(
            session,
            claim_id=claim["id"],
            event_type="review.created",
            payload={"type": "FIELD_VERIFY", "capability_id": "doc.extract"},
            actor=AGENT,
            correlation_id=None,
        )
        event_id = event.id
        session.commit()
    for _ in range(12):
        if app.state.dispatcher.dispatch_once() == 0:
            break
    with app.state.engine.connect() as connection:
        review_id = connection.execute(
            text("SELECT id FROM review_items WHERE source_event_id = :event_id"),
            {"event_id": event_id},
        ).scalar_one()
    resolved = client.post(
        f"/reviews/{review_id}/resolve",
        json={
            "action": "edit_approve",
            "schema_version": "FIELD_VERIFY@1",
            "payload": {
                "capability_id": "doc.extract",
                "diff": {
                    "typed_changes": [{"path": "vehicle.reg", "kind": "text"}],
                    "prose_change_ratio": 0,
                },
                "corrected_fields": {"vehicle.reg": "KBB 222B"},
            },
        },
        headers={"X-Actor": OFFICER},
    )
    assert resolved.status_code == 200, resolved.text
    with app.state.engine.connect() as connection:
        versions = connection.execute(
            text(
                "SELECT value, verification_state FROM claim_fields "
                "WHERE claim_id = :claim_id AND path = 'vehicle.reg' "
                "ORDER BY version"
            ),
            {"claim_id": claim["id"]},
        ).all()
    assert len(versions) == 2
    value = versions[-1][0]
    if isinstance(value, str):
        value = json.loads(value)
    assert value == "KBB 222B"
    assert versions[-1][1] == "human_verified"


def test_decline_state_is_checked_before_resolution_commit(tmp_path: Path) -> None:
    client, app, claim_id, review_id = _pending_decline(tmp_path, "stale_decline")
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET status = 'IN_APPROVAL' WHERE id = :claim_id"),
            {"claim_id": claim_id},
        )

    response = _approve_decline(client, review_id)

    assert response.status_code == 409
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    with app.state.engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM review_items WHERE id = :review_id"),
            {"review_id": review_id},
        ).scalar_one()
        resolved_events = connection.execute(
            text(
                "SELECT COUNT(*) FROM events WHERE claim_id = :claim_id "
                "AND type = 'review.resolved'"
            ),
            {"claim_id": claim_id},
        ).scalar_one()
    assert status == "open"
    assert resolved_events == 0


def test_failed_decline_commit_reopens_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, claim_id, review_id = _pending_decline(tmp_path, "failed_decline")

    def fail_decline(*_args, **_kwargs):
        raise ClaimCoreError(409, "ILLEGAL_TRANSITION", "simulated claim-state race")

    monkeypatch.setattr(app.state.claim_service, "decline_claim", fail_decline)
    response = _approve_decline(client, review_id)

    assert response.status_code == 409
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    with app.state.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, resolved_at, resolved_by, resolution, "
                "resolution_payload, resolution_schema_version "
                "FROM review_items WHERE id = :review_id"
            ),
            {"review_id": review_id},
        ).one()
    assert tuple(row) == ("open", None, None, None, None, None)
    hydrated = client.get(f"/claims/{claim_id}", headers={"X-Actor": AGENT}).json()
    assert "decline pending claims_manager approval" in hydrated["blocked_reasons"]


@pytest.mark.parametrize(
    "content",
    [
        "[",
        "roles: []\n",
        "roles:\n  agent:intake: claims_manager\n",
        "roles:\n  user:short: claims_manager\n",
        "roles:\n  user:01HREVQOFFICER00000000AAAA: ''\n",
    ],
)
def test_role_loader_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "roles.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_roles(path)


@pytest.mark.parametrize(
    "content",
    [
        "[",
        "[]\n",
        "- {role: claims_manager}\n",
        "- {role: claims_manager, max: true}\n",
        "- {role: claims_manager, max: 100.5}\n",
        "- {role: claims_manager, max: 100}\n- {role: claims_manager, max: 200}\n",
    ],
)
def test_authority_matrix_loader_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "authority_matrix.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_authority_matrix(path)
