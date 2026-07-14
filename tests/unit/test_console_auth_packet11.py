"""Focused coverage for PACKET-11's live Entra verification boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from review_queue.auth import (
    EntraSettings,
    EntraTokenVerifier,
    TokenVerificationError,
    _validate_identity_map,
)

TENANT = "11111111-1111-1111-1111-111111111111"
OID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACTOR = "user:01HCONSOLEOFFICER00000AAAA"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}/v2.0"
AUDIENCE = "api://pacha-console"


def _settings() -> EntraSettings:
    return EntraSettings(tenant_id=TENANT, audience=AUDIENCE, authority=AUTHORITY)


def _verifier(*, issuer: str = AUTHORITY, published_kid: str = "key-1"):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": published_kid, "use": "sig", "alg": "RS256"})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                request=request,
                json={"issuer": issuer, "jwks_uri": "https://login.test/keys"},
            )
        return httpx.Response(200, request=request, json={"keys": [public_jwk]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EntraTokenVerifier(_settings(), client=client), private_key, calls


def _token(private_key, *, kid: str = "key-1", **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": AUTHORITY,
        "aud": AUDIENCE,
        "tid": TENANT,
        "oid": OID,
        "nbf": now - timedelta(seconds=5),
        "exp": now + timedelta(minutes=5),
        **overrides,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def test_live_verifier_accepts_exact_signed_identity_and_caches_jwks() -> None:
    verifier, private_key, calls = _verifier()

    first = verifier.verify(_token(private_key))
    second = verifier.verify(_token(private_key))

    assert (first.tid, first.oid) == (TENANT, OID)
    assert second == first
    assert len(calls) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://other"},
        {"tid": "22222222-2222-2222-2222-222222222222"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
    ],
)
def test_live_verifier_rejects_invalid_binding_or_time(overrides: dict) -> None:
    verifier, private_key, _calls = _verifier()
    with pytest.raises(TokenVerificationError):
        verifier.verify(_token(private_key, **overrides))


def test_unknown_kid_gets_one_bounded_refresh_then_fails() -> None:
    verifier, private_key, calls = _verifier(published_kid="other-key")

    with pytest.raises(TokenVerificationError, match="unknown"):
        verifier.verify(_token(private_key, kid="missing-key"))

    assert len(calls) == 2


def test_metadata_issuer_mismatch_fails_closed() -> None:
    verifier, private_key, _calls = _verifier(issuer="https://issuer.invalid")
    with pytest.raises(TokenVerificationError, match="issuer"):
        verifier.verify(_token(private_key))


def test_environment_settings_require_exact_tenant_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PACHA_ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("PACHA_ENTRA_API_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("PACHA_ENTRA_AUTHORITY", raising=False)
    assert EntraSettings.from_environment() == _settings()

    monkeypatch.setenv("PACHA_ENTRA_AUTHORITY", "https://login.microsoftonline.com/common/v2.0")
    with pytest.raises(ValueError, match="tenant-specific"):
        EntraSettings.from_environment()


def test_identity_targets_are_unique_and_independently_roled() -> None:
    identities = {
        f"{TENANT}:{OID}": ACTOR,
        f"{TENANT}:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": ACTOR,
    }
    with pytest.raises(ValueError, match="duplicate identity"):
        _validate_identity_map(identities, {ACTOR: "claims_officer"})

    with pytest.raises(ValueError, match="no configured role"):
        _validate_identity_map({f"{TENANT}:{OID}": ACTOR}, {})


def test_installed_ingress_rejects_actor_spoofing_on_legacy_claim_routes(
    tmp_path,
) -> None:
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from review_queue import build_review_queue, install_console

    repo = Path(__file__).resolve().parents[2]
    app = create_app(f"sqlite:///{tmp_path}/actor_spoof.db")
    build_cop_runtime(app, pack_paths=[repo / "packs/motor"])
    build_review_queue(app, roles={ACTOR: "claims_officer"})
    verifier, _private_key, _calls = _verifier()
    install_console(
        app,
        verifier=verifier,
        identities={f"{TENANT}:{OID}": ACTOR},
        roles={ACTOR: "claims_officer"},
    )

    response = TestClient(app).post(
        "/claims",
        headers={"X-Actor": "agent:spoofed"},
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ACTOR_HEADER_FORBIDDEN"
