"""PACKET-11 acceptance — trusted console identity and Claim-360/citation API.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-11_console_sso.md. No broker, browser, live Entra tenant,
network call or production secret is permitted.
"""
from __future__ import annotations

import os
import pathlib

import fitz
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER = "user:01HCONSOLEOFFICER00000AAAA"
OFFICER_2 = "user:01HCONSOLEOFFICER00000BBBB"
AUDITOR = "user:01HCONSOLEAUDITOR00000AAAA"
FINANCE = "user:01HCONSOLEFINANCE00000AAAA"
UNMAPPED_ACTOR = "user:01HCONSOLEUNMAPPED0000AAAA"

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
OIDS = {
    "officer-token": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "officer-2-token": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "auditor-token": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "finance-token": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "unmapped-token": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
}
ROLES = {
    OFFICER: "claims_officer",
    OFFICER_2: "claims_officer",
    AUDITOR: "auditor",
    FINANCE: "finance",
}
IDENTITIES = {
    f"{TENANT}:{OIDS['officer-token']}": OFFICER,
    f"{TENANT}:{OIDS['officer-2-token']}": OFFICER_2,
    f"{TENANT}:{OIDS['auditor-token']}": AUDITOR,
    f"{TENANT}:{OIDS['finance-token']}": FINANCE,
}


class FakeVerifier:
    """Pinned TokenVerifier seam; any unexpected token is invalid, never decoded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, token: str):
        from review_queue.auth import TokenClaims, TokenVerificationError

        self.calls.append(token)
        if token == "invalid-token":
            raise TokenVerificationError("fixture signature failure")
        if token == "wrong-tenant-token":
            return TokenClaims(tid=OTHER_TENANT, oid=OIDS["officer-token"])
        oid = OIDS.get(token)
        if oid is None:
            raise TokenVerificationError("unknown fixture token")
        return TokenClaims(tid=TENANT, oid=oid)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _pdf(label: str = "KAA 111A") -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 160), label)
    content = document.tobytes()
    document.close()
    return content


def _drain(app, cycles: int = 16) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _emit(app, claim_id: str, event_type: str, payload: dict) -> str:
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


@pytest.fixture()
def env(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core import FieldWrite, create_app
    from claim_core.schemas import ClaimCreate
    from cop_runtime import build_cop_runtime
    from review_queue import build_review_queue, install_console

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc11.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_review_queue(app, roles=dict(ROLES))

    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), AGENT
    )
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :actor WHERE id = :claim_id"),
            {"actor": OFFICER, "claim_id": claim.id},
        )
    source = _pdf()
    document = app.state.claim_service.add_document(
        claim.id,
        filename="source.pdf",
        mime="application/pdf",
        content=source,
        source_channel="test",
        source_ref="packet-11",
        actor=AGENT,
    )
    app.state.blob_store.put(f"normalised/{document.id}.pdf", source)
    citation = {
        "document_id": document.id,
        "page": 1,
        "bbox": [0.10, 0.20, 0.40, 0.30],
        "anchor_text": "KAA 111A",
        "citation_mode": "anchor_text",
    }
    app.state.claim_service.write_fields(
        claim.id,
        [
            FieldWrite(
                path="vehicle.reg",
                value="KAA 111A",
                value_type="string",
                source_type="extraction",
                source_ref=citation,
                confidence="0.97",
                verification_state="extracted",
            ),
            FieldWrite(
                path="parties.insured.name",
                value="Amina Wanjiku",
                value_type="string",
                source_type="extraction",
                source_ref=citation,
                confidence="0.96",
                verification_state="extracted",
            ),
        ],
        AGENT,
    )
    app.state.claim_service.write_fields(
        claim.id,
        [
            FieldWrite(
                path="reserve.total",
                value=1_234_56,
                value_type="money",
                source_type="human",
                source_ref={"user_id": OFFICER, "review_item_id": "fixture"},
                verification_state="human_verified",
            )
        ],
        OFFICER,
    )
    # The current human version intentionally has no document citation. The
    # console must retain append-only evidence lineage from the extraction.
    app.state.claim_service.write_fields(
        claim.id,
        [
            FieldWrite(
                path="vehicle.reg",
                value="KAA 111B",
                value_type="string",
                source_type="human",
                source_ref={"user_id": OFFICER, "review_item_id": "fixture-correction"},
                verification_state="human_verified",
            )
        ],
        OFFICER,
    )
    review_event_id = _emit(
        app,
        claim.id,
        "review.created",
        {
            "type": "FIELD_VERIFY",
            "path": "vehicle.reg",
            "candidate_value": "KAA 111B",
            "capability_id": "doc.extract.registration",
            "citations": [citation],
        },
    )
    _emit(app, claim.id, "projection.completed", {"system": "icon", "status": "completed"})
    _emit(app, claim.id, "email.received", {"thread_id": "thread-1", "subject": "Claim"})
    _drain(app)

    verifier = FakeVerifier()
    install_console(
        app,
        verifier=verifier,
        identities=dict(IDENTITIES),
        roles=dict(ROLES),
    )
    client = TestClient(app)
    return {
        "client": client,
        "app": app,
        "claim_id": claim.id,
        "document_id": document.id,
        "review_event_id": review_event_id,
        "verifier": verifier,
    }


def test_console_rejects_missing_raw_and_spoofed_actor_identity(env):
    client = env["client"]
    missing = client.get("/reviews")
    assert missing.status_code == 401
    assert missing.json()["code"] == "AUTHENTICATION_REQUIRED"

    raw = client.get("/reviews", headers={"X-Actor": OFFICER})
    assert raw.status_code == 400
    assert raw.json()["code"] == "ACTOR_HEADER_FORBIDDEN"

    spoofed = client.get(
        "/reviews",
        headers={**_bearer("officer-token"), "X-Actor": OFFICER_2},
    )
    assert spoofed.status_code == 400
    assert spoofed.json()["code"] == "ACTOR_HEADER_FORBIDDEN"


@pytest.mark.parametrize("token", ["invalid-token", "wrong-tenant-token", "unmapped-token"])
def test_invalid_or_unmapped_identity_fails_closed(env, token):
    response = env["client"].get("/auth/me", headers=_bearer(token))
    expected = 401 if token == "invalid-token" else 403
    assert response.status_code == expected
    assert response.json()["code"] in {"INVALID_TOKEN", "IDENTITY_NOT_MAPPED"}


def test_auth_me_exposes_only_internal_actor_and_independent_role(env):
    response = env["client"].get("/auth/me", headers=_bearer("officer-token"))
    assert response.status_code == 200, response.text
    assert response.json() == {"actor": OFFICER, "role": "claims_officer"}
    assert OIDS["officer-token"] not in response.text
    assert TENANT not in response.text


def test_verified_actor_drives_mine_scope_and_resolution_attribution(env):
    client = env["client"]
    queue = client.get("/reviews", headers=_bearer("officer-token"))
    assert queue.status_code == 200, queue.text
    [item] = [row for row in queue.json()["items"] if row["claim_id"] == env["claim_id"]]
    assert item["workspace_layout"] == "field_verify"
    assert item["resolution_schema"] == "FIELD_VERIFY@1"

    resolved = client.post(
        f"/reviews/{item['id']}/resolve",
        headers=_bearer("officer-token"),
        json={
            "action": "approve",
            "schema_version": "FIELD_VERIFY@1",
            "payload": {
                "capability_id": "doc.extract.registration",
                "diff": {"typed_changes": [], "prose_change_ratio": 0},
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved_by"] == OFFICER


def test_s1_s2_role_policy_is_fail_closed_and_auditor_is_read_only(env):
    client = env["client"]
    denied = client.get(
        f"/console/claims/{env['claim_id']}/360",
        headers=_bearer("finance-token"),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_ROLE"

    allowed = client.get(
        f"/console/claims/{env['claim_id']}/360",
        headers=_bearer("auditor-token"),
    )
    assert allowed.status_code == 200, allowed.text

    items = client.get("/reviews", headers=_bearer("auditor-token")).json()["items"]
    item = next(row for row in items if row["claim_id"] == env["claim_id"])
    write = client.post(
        f"/reviews/{item['id']}/resolve",
        headers=_bearer("auditor-token"),
        json={
            "action": "approve",
            "schema_version": "FIELD_VERIFY@1",
            "payload": {
                "capability_id": "doc.extract.registration",
                "diff": {"typed_changes": [], "prose_change_ratio": 0},
            },
        },
    )
    assert write.status_code == 403
    assert write.json()["code"] == "FORBIDDEN_ROLE"


def test_claim_360_is_coherent_money_exact_and_unbuilt_sections_visible(env):
    response = env["client"].get(
        f"/console/claims/{env['claim_id']}/360",
        headers=_bearer("officer-token"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["claim"]["id"] == env["claim_id"]
    assert body["header"] == {
        "insured": "Amina Wanjiku",
        "registration": "KAA 111B",
        "amount_cents": "123456",
    }
    money = next(row for row in body["financials"] if row["path"] == "reserve.total")
    assert money["amount_cents"] == "123456"
    assert isinstance(money["amount_cents"], str)
    assert next(row for row in body["fields"] if row["path"] == "vehicle.reg")[
        "has_citation"
    ] is True
    assert body["systems"] == [
        {"system": "icon", "status": "completed", "event_type": "projection.completed"}
    ]
    assert body["communications"][0]["event_type"] == "email.received"
    assert body["availability"]["document_checklist"] == {
        "status": "not_available",
        "owner": "PRD-06",
    }


def test_citation_uses_current_value_and_historical_exact_bbox(env):
    response = env["client"].get(
        f"/console/claims/{env['claim_id']}/fields/vehicle.reg/citation",
        headers=_bearer("officer-token"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "claim_id": env["claim_id"],
        "field_path": "vehicle.reg",
        "value": "KAA 111B",
        "value_type": "string",
        "verification_state": "human_verified",
        "document_id": env["document_id"],
        "page": 1,
        "bbox": [0.1, 0.2, 0.4, 0.3],
        "document_url": f"/console/documents/{env['document_id']}/normalised.pdf",
    }


def test_citation_refuses_missing_field_or_missing_render(env):
    client = env["client"]
    missing = client.get(
        f"/console/claims/{env['claim_id']}/fields/loss.date/citation",
        headers=_bearer("officer-token"),
    )
    assert missing.status_code == 409
    assert missing.json()["code"] == "CITATION_UNAVAILABLE"

    render_path = (
        pathlib.Path(env["app"].state.blob_store.root)
        / "normalised"
        / f"{env['document_id']}.pdf"
    )
    render_path.unlink()
    no_render = client.get(
        f"/console/claims/{env['claim_id']}/fields/vehicle.reg/citation",
        headers=_bearer("officer-token"),
    )
    assert no_render.status_code == 409
    assert no_render.json()["code"] == "CITATION_UNAVAILABLE"


def test_normalised_pdf_route_is_private_nosniff_and_not_arbitrary_blob_access(env):
    response = env["client"].get(
        f"/console/documents/{env['document_id']}/normalised.pdf",
        headers=_bearer("officer-token"),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"%PDF")

    arbitrary = env["client"].get(
        "/console/documents/../../audit/ledger/normalised.pdf",
        headers=_bearer("officer-token"),
    )
    assert arbitrary.status_code in {404, 422}


def test_identity_config_rejects_duplicate_actor_targets(tmp_path):
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from review_queue import build_review_queue, install_console

    app = create_app(f"sqlite:///{tmp_path}/duplicate_identity.db")
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_review_queue(app, roles=dict(ROLES))
    with pytest.raises(ValueError, match="duplicate|identity"):
        install_console(
            app,
            verifier=FakeVerifier(),
            identities={
                f"{TENANT}:{OIDS['officer-token']}": OFFICER,
                f"{TENANT}:{OIDS['officer-2-token']}": OFFICER,
            },
            roles=dict(ROLES),
        )
