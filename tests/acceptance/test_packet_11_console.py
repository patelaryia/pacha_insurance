"""PACKET-11 acceptance — PRD-04 §4.1/§4.2 console server: SSO identity,
static shell, read-only console API.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-11_console_shell.md §3.1. Token validation is proven
offline via an injected JWKS; no browser, broker, network, or live model
call is permitted. The frontend half of this packet's acceptance lives in
tests/acceptance/console/packet_11_*.test.tsx (vitest, CI console job).
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import pathlib
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER = "user:01HCONSOLEOFFICER00000AAAA"
ACM = "user:01HCONSOLEASSTMANAGER0AAAA"
CM = "user:01HCONSOLECLAIMSMGR000AAAA"
AUDITOR = "user:01HCONSOLEAUDITOR00000AAAA"

ROLES = {
    OFFICER: "claims_officer",
    ACM: "asst_claims_manager",
    CM: "claims_manager",
    AUDITOR: "auditor",
}

OID_OFFICER = "11111111-1111-1111-1111-111111111111"
OID_ACM = "22222222-2222-2222-2222-222222222222"
OID_CM = "33333333-3333-3333-3333-333333333333"
OID_AUDITOR = "44444444-4444-4444-4444-444444444444"
OID_GHOST = "99999999-9999-9999-9999-999999999999"

USERS = {
    OID_OFFICER: OFFICER,
    OID_ACM: ACM,
    OID_CM: CM,
    OID_AUDITOR: AUDITOR,
}

ISSUER = "https://login.microsoftonline.com/test-tenant/v2.0"
AUDIENCE = "api://pacha-console-test"
KID = "packet-11-test-key"

PRIMARY_PATH = [
    "INTIMATED", "TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT",
    "REPORT_RECEIVED", "REGISTERED", "RESERVED", "PACK_READY",
    "IN_APPROVAL", "APPROVED", "IN_REPAIR", "REINSPECTION", "RELEASED",
    "SETTLEMENT", "SETTLED", "CLOSED",
]
WRITE_OFF_PATH = [
    "REPORT_RECEIVED", "WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION",
]
TERMINAL = {"CLOSED", "DECLINED", "WITHDRAWN", "VOID"}


# --- offline RS256 token plumbing ---------------------------------------------------


def _rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PRIVATE_KEY = _rsa_key()


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwks() -> dict:
    numbers = PRIVATE_KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KID,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


def _token(
    oid: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_in: int = 600,
    kid: str = KID,
) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
            "sub": f"sub-{oid}",
            "oid": oid,
        },
        PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _bearer(oid: str, **kwargs) -> dict:
    return {"Authorization": f"Bearer {_token(oid, **kwargs)}"}


def _h(actor: str) -> dict:
    return {"X-Actor": actor}


# --- app assembly -------------------------------------------------------------------


def _dist(tmp_path) -> pathlib.Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>pacha console shell</body></html>"
    )
    (dist / "assets" / "app.js").write_text("/* packet-11 asset */")
    return dist


def _build(tmp_path, name: str, *, oidc: bool = True):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_console, build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles=dict(ROLES))
    oidc_config = None
    if oidc:
        from review_queue.sso import OIDCConfig

        oidc_config = OIDCConfig(issuer=ISSUER, audience=AUDIENCE, jwks=_jwks())
    build_console(
        app,
        roles=dict(ROLES),
        users=dict(USERS),
        oidc=oidc_config,
        dist_path=_dist(tmp_path),
    )
    return TestClient(app), app


@pytest.fixture()
def env(tmp_path):
    return _build(tmp_path, "pacha_acc11")


@pytest.fixture()
def header_env(tmp_path):
    return _build(tmp_path, "pacha_acc11_header", oidc=False)


# --- shared seeding (PACKET-10 mechanics, unchanged) --------------------------------


def _claim(client) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, claim_id: str, label: str = "source") -> tuple[str, bytes]:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(image).text((40, 100), label, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    response = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (f"{label}.png", io.BytesIO(content), "image/png")},
        data={"source_channel": "test", "source_ref": f"msg-{label}"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], content


def _assign(app, claim_id: str, officer: str) -> None:
    from sqlalchemy import text

    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :officer WHERE id = :claim_id"),
            {"officer": officer, "claim_id": claim_id},
        )


def _emit(app, event_type: str, payload: dict, claim_id: str | None = None) -> str:
    from sqlalchemy.orm import Session

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


def _item_id(app, source_event_id: str) -> str:
    from sqlalchemy import text

    with app.state.engine.connect() as connection:
        item_id = connection.execute(
            text("SELECT id FROM review_items WHERE source_event_id = :event_id"),
            {"event_id": source_event_id},
        ).scalar()
    assert item_id is not None, "review item was not projected"
    return str(item_id)


def _note_item(app, claim_id: str) -> str:
    event_id = _emit(
        app,
        "review.created",
        {
            "type": "NOTE_REVIEW",
            "capability_id": "pack.note_draft",
            "output": {"note": "draft"},
            "citations": [{"document_id": "d", "page": 1}],
        },
        claim_id,
    )
    _drain(app)
    return _item_id(app, event_id)


def _pack_review_item(app, claim_id: str) -> str:
    event_id = _emit(
        app,
        "review.created",
        {
            "type": "PACK_REVIEW",
            "capability_id": "pack.note_draft",
            "output": {"pack": "draft"},
            "citations": [{"document_id": "d", "page": 1}],
        },
        claim_id,
    )
    _drain(app)
    return _item_id(app, event_id)


def _human_money(client, claim_id: str, path: str, cents: int) -> None:
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": cents,
                    "value_type": "money",
                    "source_type": "human",
                    "verification_state": "human_verified",
                }
            ]
        },
        headers=_h(CM),
    )
    assert response.status_code == 200, response.text


def _resolve_body(*, reason: str | None = None) -> dict:
    payload: dict = {
        "capability_id": "pack.note_draft",
        "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _ledger_actions(app) -> list[str]:
    from sqlalchemy import text

    with app.state.engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT action FROM audit_ledger ORDER BY seq")
            )
        ]


# --- static shell + config ----------------------------------------------------------


def test_static_shell_and_config(env):
    client, _app = env
    index = client.get("/console/")
    assert index.status_code == 200
    assert "pacha console shell" in index.text
    asset = client.get("/console/assets/app.js")
    assert asset.status_code == 200
    assert "packet-11 asset" in asset.text

    config = client.get("/api/console/config")
    assert config.status_code == 200
    body = config.json()
    assert body["auth_mode"] == "oidc"
    assert body["sso_status"] in {"configured", "pending_capture"}
    flattened = str(body).lower()
    assert "secret" not in flattened
    assert "private" not in flattened


def test_header_mode_config_and_packet_10_contract_intact(header_env):
    client, app = header_env
    config = client.get("/api/console/config")
    assert config.status_code == 200
    assert config.json()["auth_mode"] == "header"

    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    item_id = _note_item(app, claim_id)
    response = client.post(
        f"/reviews/{item_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "NOTE_REVIEW@1",
            "payload": _resolve_body(),
        },
        headers=_h(OFFICER),
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution"] == "approved"


# --- FSM topology -------------------------------------------------------------------


def test_fsm_topology_endpoint(env):
    client, _app = env
    response = client.get("/api/console/fsm", headers=_bearer(OID_OFFICER))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["primary_path"] == PRIMARY_PATH
    assert body["write_off_path"][: len(WRITE_OFF_PATH)] == WRITE_OFF_PATH
    assert set(body["terminal"]) == TERMINAL
    from claim_core.fsm import ClaimState

    known = {state.value for state in ClaimState}
    for listed in (
        set(body["primary_path"]) | set(body["write_off_path"]) | set(body["terminal"])
    ):
        assert listed in known, f"invented state {listed}"


# --- bearer identity ----------------------------------------------------------------


def test_bearer_token_maps_actor_and_scope_mine(env):
    client, app = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    _note_item(app, claim_id)

    mine = client.get("/reviews?scope=mine", headers=_bearer(OID_OFFICER))
    assert mine.status_code == 200, mine.text
    items = mine.json()["items"]
    assert len(items) == 1
    assert items[0]["claim_id"] == claim_id
    assert items[0]["assigned_to"] == OFFICER

    other = client.get("/reviews?scope=mine", headers=_bearer(OID_ACM))
    assert other.status_code == 200
    assert other.json()["items"] == []


def test_invalid_tokens_401(env):
    client, _app = env
    for headers in (
        _bearer(OID_OFFICER, expires_in=-60),
        _bearer(OID_OFFICER, audience="api://wrong"),
        _bearer(OID_OFFICER, issuer="https://evil.example/v2.0"),
        _bearer(OID_OFFICER, kid="unknown-kid"),
        {"Authorization": "Bearer not.a.jwt"},
    ):
        response = client.get("/reviews?scope=pool", headers=headers)
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "TOKEN_INVALID"


def test_unmapped_oid_403_ledgered(env):
    client, app = env
    before = _ledger_actions(app).count("authz.denied")
    response = client.get("/reviews?scope=pool", headers=_bearer(OID_GHOST))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "FORBIDDEN_ROLE"
    _drain(app)
    assert _ledger_actions(app).count("authz.denied") == before + 1


def test_bearer_mode_locks_out_header_humans_not_agents(env):
    client, _app = env
    response = client.get("/reviews?scope=pool", headers=_h(OFFICER))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "TOKEN_REQUIRED"

    created = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert created.status_code == 201, created.text


def test_users_mapping_must_target_user_actors(tmp_path):
    from review_queue.sso import OIDCConfig

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_console, build_review_queue

    url = os.environ.get(
        "DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc11_badmap.db"
    )
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles=dict(ROLES))
    with pytest.raises(ValueError):
        build_console(
            app,
            roles=dict(ROLES),
            users={OID_OFFICER: "agent:intake"},
            oidc=OIDCConfig(issuer=ISSUER, audience=AUDIENCE, jwks=_jwks()),
            dist_path=_dist(tmp_path),
        )


# --- authorisation parity through bearer identity ------------------------------------


def test_resolve_parity_and_band_boundary_bearer(env):
    client, app = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    _human_money(client, claim_id, "assessment.agreed_quote", 100_000_00)
    item_id = _pack_review_item(app, claim_id)

    at_boundary = client.post(
        f"/reviews/{item_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "PACK_REVIEW@1",
            "payload": _resolve_body(),
        },
        headers=_bearer(OID_ACM),
    )
    assert at_boundary.status_code == 200, at_boundary.text
    assert at_boundary.json()["resolved_by"] == ACM

    claim_2 = _claim(client)
    _assign(app, claim_2, OFFICER)
    _human_money(client, claim_2, "assessment.agreed_quote", 100_000_01)
    item_2 = _pack_review_item(app, claim_2)
    before = _ledger_actions(app).count("authz.denied")
    above = client.post(
        f"/reviews/{item_2}/resolve",
        json={
            "action": "approve",
            "schema_version": "PACK_REVIEW@1",
            "payload": _resolve_body(),
        },
        headers=_bearer(OID_ACM),
    )
    assert above.status_code == 403, above.text
    assert above.json()["code"] == "FORBIDDEN_BAND"
    _drain(app)
    assert _ledger_actions(app).count("authz.denied") == before + 1


def test_auditor_read_only_via_bearer(env):
    client, app = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    item_id = _note_item(app, claim_id)

    listing = client.get("/reviews?scope=pool", headers=_bearer(OID_AUDITOR))
    assert listing.status_code == 200

    denied = client.post(
        f"/reviews/{item_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "NOTE_REVIEW@1",
            "payload": _resolve_body(),
        },
        headers=_bearer(OID_AUDITOR),
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "FORBIDDEN_ROLE"


# --- read-only console API ----------------------------------------------------------


def test_document_blob_roundtrip(env):
    client, app = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    document_id, content = _upload(client, claim_id, "citation-source")

    blob = client.get(
        f"/api/console/documents/{document_id}/blob",
        headers=_bearer(OID_OFFICER),
    )
    assert blob.status_code == 200, blob.text
    assert blob.content == content
    assert blob.headers["content-type"].startswith("image/png")
    assert hashlib.sha256(content).hexdigest() in blob.headers.get("etag", "")

    missing = client.get(
        "/api/console/documents/01HNOSUCHDOCUMENT00000AAAA/blob",
        headers=_bearer(OID_OFFICER),
    )
    assert missing.status_code == 404


def test_calc_runs_read_endpoint(env):
    client, app = env
    claim_id = _claim(client)
    _assign(app, claim_id, OFFICER)
    response = client.get(
        f"/api/console/claims/{claim_id}/calc-runs",
        headers=_bearer(OID_OFFICER),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"runs": []}


def test_console_api_is_read_only_and_documented(env):
    client, _app = env
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    console_paths = {path: spec for path, spec in paths.items()
                     if path.startswith("/api/console/")}
    assert any("/api/console/fsm" == path for path in console_paths)
    assert any("blob" in path for path in console_paths)
    assert any("calc-runs" in path for path in console_paths)
    for path, spec in console_paths.items():
        writes = {"post", "put", "patch", "delete"} & set(spec)
        assert not writes, f"write method {writes} under read-only {path}"
