"""Fail-closed Microsoft Entra identity boundary for the staff console."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx
import jwt
import yaml
from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from review_queue.console_api import build_console_router
from review_queue.rbac import USER_ACTOR

PROTECTED_PREFIXES = ("/auth/", "/reviews", "/console/")
CONSOLE_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "gm",
        "md",
        "chairman",
        "head_of_claims",
        "auditor",
    }
)
OPS_INGRESS_ROLES = CONSOLE_ROLES | {"admin"}
GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class TokenClaims:
    tid: str
    oid: str


class TokenVerificationError(ValueError):
    """The access token could not be cryptographically trusted."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> TokenClaims:
        """Verify one bearer token and return only its immutable identity."""


@dataclass(frozen=True)
class EntraSettings:
    tenant_id: str
    audience: str
    authority: str

    @classmethod
    def from_environment(cls) -> EntraSettings:
        tenant_id = os.getenv("PACHA_ENTRA_TENANT_ID", "").strip().lower()
        audience = os.getenv("PACHA_ENTRA_API_AUDIENCE", "").strip()
        authority = os.getenv("PACHA_ENTRA_AUTHORITY", "").strip().rstrip("/")
        if not GUID.fullmatch(tenant_id):
            raise ValueError("PACHA_ENTRA_TENANT_ID must be a canonical tenant GUID")
        if not audience:
            raise ValueError("PACHA_ENTRA_API_AUDIENCE is required")
        expected_authority = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        if not authority:
            authority = expected_authority
        if authority != expected_authority:
            raise ValueError("PACHA_ENTRA_AUTHORITY must be the tenant-specific v2 authority")
        return cls(tenant_id=tenant_id, audience=audience, authority=authority)


class EntraTokenVerifier:
    """RS256 verifier backed by tenant-specific OIDC metadata and JWKS."""

    def __init__(
        self,
        settings: EntraSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0))
        self._issuer: str | None = None
        self._jwks_uri: str | None = None
        self._keys: dict[str, Any] = {}

    def _refresh(self) -> None:
        metadata_url = (
            f"{self.settings.authority}/.well-known/openid-configuration"
        )
        try:
            metadata_response = self._client.get(metadata_url)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            issuer = metadata["issuer"]
            jwks_uri = metadata["jwks_uri"]
            if issuer != self.settings.authority:
                raise TokenVerificationError("OIDC issuer does not match configured authority")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                raise TokenVerificationError("OIDC JWKS URI is invalid")
            jwks_response = self._client.get(jwks_uri)
            jwks_response.raise_for_status()
            keys = jwks_response.json().get("keys")
            if not isinstance(keys, list) or not keys:
                raise TokenVerificationError("OIDC JWKS contains no signing keys")
            parsed: dict[str, Any] = {}
            for key in keys:
                if not isinstance(key, dict) or key.get("kty") != "RSA":
                    continue
                kid = key.get("kid")
                if not isinstance(kid, str) or not kid or kid in parsed:
                    raise TokenVerificationError("OIDC JWKS contains an invalid key id")
                parsed[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            if not parsed:
                raise TokenVerificationError("OIDC JWKS contains no supported signing key")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, TokenVerificationError):
                raise
            raise TokenVerificationError("OIDC metadata or JWKS could not be loaded") from error
        self._issuer = issuer
        self._jwks_uri = jwks_uri
        self._keys = parsed

    def verify(self, token: str) -> TokenClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise TokenVerificationError("Access token header is invalid") from error
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise TokenVerificationError("Access token signing header is invalid")
        if kid not in self._keys:
            self._refresh()
        key = self._keys.get(kid)
        if key is None:
            # The bounded refresh above is the sole unknown-kid refresh attempt.
            raise TokenVerificationError("Access token signing key is unknown")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self._issuer,
                options={"require": ["exp", "nbf", "tid", "oid"]},
            )
        except jwt.PyJWTError as error:
            raise TokenVerificationError("Access token validation failed") from error
        tid = claims.get("tid")
        oid = claims.get("oid")
        if tid != self.settings.tenant_id or not _canonical_guid(oid):
            raise TokenVerificationError("Access token immutable identity is invalid")
        return TokenClaims(tid=tid, oid=oid)


def _canonical_guid(value: Any) -> bool:
    if not isinstance(value, str) or GUID.fullmatch(value) is None:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _load_identities(path: Path) -> dict[str, str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid identity config: {error}") from error
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("identity config requires version 1")
    identities = raw.get("identities")
    if not isinstance(identities, dict):
        raise ValueError("identity config identities must be a mapping")
    return dict(identities)


def _validate_identity_map(
    identities: dict[str, str], roles: dict[str, str]
) -> dict[str, str]:
    checked: dict[str, str] = {}
    targets: set[str] = set()
    for external, actor in identities.items():
        if not isinstance(external, str) or external.count(":") != 1:
            raise ValueError("identity key must be '<tenant-guid>:<object-guid>'")
        tid, oid = external.split(":", 1)
        if not _canonical_guid(tid) or not _canonical_guid(oid):
            raise ValueError("identity key contains a malformed immutable identity")
        if not isinstance(actor, str) or USER_ACTOR.fullmatch(actor) is None:
            raise ValueError("identity target must be a user ULID actor")
        if actor in targets:
            raise ValueError("duplicate identity actor target")
        role = roles.get(actor)
        if not isinstance(role, str) or not role:
            raise ValueError("identity actor has no configured role")
        checked[external] = actor
        targets.add(actor)
    return checked


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "detail": detail})


class ConsoleIngressMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        *,
        verifier: TokenVerifier,
        identities: dict[str, str],
        roles: dict[str, str],
    ) -> None:
        super().__init__(app)
        self.verifier = verifier
        self.identities = identities
        self.roles = roles

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        headers = Headers(scope=request.scope)
        # Register #100 retires the network actor transport for the whole app as
        # soon as console ingress is installed. Checking this before the route
        # allow-list prevents legacy claim/event endpoints becoming a spoofing
        # side door while machine ingress remains an infra-owned concern.
        if "x-actor" in headers:
            return _error(400, "ACTOR_HEADER_FORBIDDEN", "X-Actor is not accepted")
        if not any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
            return await call_next(request)
        authorization = headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return _error(
                401,
                "AUTHENTICATION_REQUIRED",
                "A bearer access token is required",
            )
        try:
            claims = self.verifier.verify(token.strip())
        except TokenVerificationError:
            return _error(401, "INVALID_TOKEN", "The access token is invalid")
        except Exception:
            return _error(401, "INVALID_TOKEN", "The access token could not be verified")
        actor = self.identities.get(f"{claims.tid}:{claims.oid}")
        if actor is None:
            return _error(403, "IDENTITY_NOT_MAPPED", "Identity is not mapped")
        role = self.roles.get(actor)
        if role is None:
            return _error(403, "FORBIDDEN_ROLE", "Actor has no configured role")
        if path.startswith("/console/ops/"):
            if role not in OPS_INGRESS_ROLES:
                return _error(403, "FORBIDDEN_ROLE", "Role has no operations access")
        elif path.startswith("/reviews") or path.startswith("/console/"):
            if role not in CONSOLE_ROLES:
                return _error(403, "FORBIDDEN_ROLE", "Role has no S-1/S-2 access")
        request.scope["headers"] = [
            *request.scope["headers"],
            (b"x-actor", actor.encode("ascii")),
        ]
        request.state.console_actor = actor
        request.state.console_role = role
        return await call_next(request)


def install_console(
    app: FastAPI,
    *,
    verifier: TokenVerifier | None = None,
    identities: dict[str, str] | None = None,
    roles: dict[str, str] | None = None,
) -> Any:
    """Install trusted console ingress and the S-1/S-2 read routes."""

    queue = getattr(app.state, "review_queue", None)
    if queue is None:
        raise ValueError("build_review_queue must run before install_console")
    repo = Path(__file__).resolve().parents[2]
    configured_roles = dict(queue.roles) if roles is None else dict(roles)
    configured_identities = (
        _load_identities(repo / "packs/motor/routing/identities.yaml")
        if identities is None
        else dict(identities)
    )
    checked_identities = _validate_identity_map(configured_identities, configured_roles)
    effective_verifier = verifier or EntraTokenVerifier(EntraSettings.from_environment())
    # Installed console roles replace the interim PACKET-10 human transport map.
    queue.service.authorizer.roles = dict(configured_roles)
    app.state.console_identities = checked_identities
    app.state.console_roles = configured_roles
    app.state.console_verifier = effective_verifier
    app.include_router(build_console_router(app))
    app.add_middleware(
        ConsoleIngressMiddleware,
        verifier=effective_verifier,
        identities=checked_identities,
        roles=configured_roles,
    )
    return app


__all__ = [
    "EntraSettings",
    "EntraTokenVerifier",
    "TokenClaims",
    "TokenVerificationError",
    "TokenVerifier",
    "install_console",
]
