"""The authenticated internal control API the runner pulls from (PACKET-21 §7).

These routes are mounted **only** when an infra-supplied `RunnerAuthenticator`
is injected. With no authenticator the application reports
`blocked_on_inputs: runner-machine-identity` and mounts nothing, so there is no
insecure path to fall back onto (register #277).

No route accepts `X-Actor`, a console OIDC user token, a query-string token, a
target-system credential, or a caller-supplied claim id. The runner proves it is
the current lease holder by presenting the raw lease token it received once, in
the claim response, over TLS.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from claim_core import ClaimCoreError
from projection_agent.adapters import AdapterContractError, OpResult

INTERNAL_PREFIX = "/internal/projection-runner"
LEASE_HEADER = "X-Runner-Lease"
PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}
#: Headers a runner route must never accept as identity.
FORBIDDEN_HEADERS = ("x-actor", "authorization")


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    systems: list[str] = Field(min_length=1)


class RunnerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_version: str
    browser_version: str
    systems: list[str] = Field(default_factory=list)
    health: str


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str
    #: Base64 PNG bytes. The manifest digest is computed server-side.
    content_base64: str


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str
    last_completed_step: str | None = None
    write_ids: list[str] = Field(default_factory=list)
    readback_keys: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    reason_code: str | None = None


def _private(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code, headers=PRIVATE_HEADERS)


def build_runner_router(service: Any) -> APIRouter:
    """Return the five authenticated internal runner routes."""

    router = APIRouter(prefix=INTERNAL_PREFIX, tags=["projection-runner"])
    coordinator = service.rpa

    def _runner(request: Request) -> str:
        headers = {key.lower(): value for key, value in request.headers.items()}
        if any(name in headers for name in FORBIDDEN_HEADERS):
            raise ClaimCoreError(
                403, "RUNNER_IDENTITY_INVALID", "A runner never presents a user identity"
            )
        if request.url.query:
            raise ClaimCoreError(
                403, "RUNNER_IDENTITY_INVALID", "A runner never presents a query-string token"
            )
        return service.runner_authenticator.authenticate(headers)

    @router.post("/jobs/claim")
    def claim(
        body: ClaimRequest,
        request: Request,
    ) -> JSONResponse:
        runner_id = _runner(request)
        grant = coordinator.claim_job(runner_id=runner_id, systems=tuple(body.systems))
        if grant is None:
            return _private({"job": None}, status_code=200)
        return _private({"job": service.lease_response(grant)})

    @router.post("/jobs/{projection_id}/heartbeat")
    def heartbeat(
        projection_id: str,
        request: Request,
        x_runner_lease: str = Header(alias=LEASE_HEADER),
    ) -> JSONResponse:
        runner_id = _runner(request)
        return _private(
            coordinator.heartbeat(projection_id, token=x_runner_lease, runner_id=runner_id)
        )

    @router.post("/jobs/{projection_id}/steps/{step_id}/evidence")
    def evidence(
        projection_id: str,
        step_id: str,
        body: EvidenceRequest,
        request: Request,
        x_runner_lease: str = Header(alias=LEASE_HEADER),
    ) -> JSONResponse:
        import base64

        runner_id = _runner(request)
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, TypeError) as error:
            raise ClaimCoreError(
                422, "EVIDENCE_CONTENT_INVALID", "Evidence bytes are not valid base64"
            ) from error
        return _private(
            coordinator.record_frame(
                projection_id,
                token=x_runner_lease,
                runner_id=runner_id,
                step_id=step_id,
                phase=body.phase,
                content=content,
            )
        )

    @router.post("/jobs/{projection_id}/result")
    def result(
        projection_id: str,
        body: ResultRequest,
        request: Request,
        x_runner_lease: str = Header(alias=LEASE_HEADER),
    ) -> JSONResponse:
        runner_id = _runner(request)
        try:
            parsed = OpResult.from_mapping(body.model_dump())
        except AdapterContractError as error:
            raise ClaimCoreError(422, "ADAPTER_RESULT_INVALID", str(error)) from error
        return _private(
            coordinator.record_result(
                projection_id,
                token=x_runner_lease,
                runner_id=runner_id,
                result=parsed,
            )
        )

    @router.post("/heartbeat")
    def runner_heartbeat(body: RunnerHeartbeat, request: Request) -> JSONResponse:
        runner_id = _runner(request)
        return _private(coordinator.record_runner_heartbeat(runner_id, body.model_dump()))

    return router


class InProcessControlPlane:
    """A `ControlPlane` bound directly to one application's coordinator.

    The runner container talks HTTPS; this transport exists so the control-plane
    acceptance suite can drive the same coordinator without a network. It grants
    no capability the HTTP routes do not, and still requires an authenticator.
    """

    def __init__(self, service: Any, *, runner_id: str) -> None:
        if service.runner_authenticator is None:
            raise ClaimCoreError(
                409,
                "PROJECTION_RUNNER_BLOCKED",
                "blocked_on_inputs: runner-machine-identity",
            )
        self.service = service
        self.coordinator = service.rpa
        self.runner_id = runner_id

    def claim(self, *, runner_id: str, systems: tuple[str, ...]) -> dict[str, Any] | None:
        grant = self.coordinator.claim_job(runner_id=runner_id, systems=systems)
        if grant is None:
            return None
        return self.service.lease_response(grant, include_token=True)

    def heartbeat(self, projection_id: str, *, token: str, runner_id: str) -> dict[str, Any]:
        return self.coordinator.heartbeat(projection_id, token=token, runner_id=runner_id)

    def upload_evidence(
        self,
        projection_id: str,
        step_id: str,
        *,
        token: str,
        runner_id: str,
        phase: str,
        content: bytes,
    ) -> dict[str, Any]:
        return self.coordinator.record_frame(
            projection_id,
            token=token,
            runner_id=runner_id,
            step_id=step_id,
            phase=phase,
            content=content,
        )

    def post_result(
        self, projection_id: str, *, token: str, runner_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return self.coordinator.record_result(
            projection_id,
            token=token,
            runner_id=runner_id,
            result=OpResult.from_mapping(result),
        )

    def runner_heartbeat(self, *, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.coordinator.record_runner_heartbeat(runner_id, payload)


__all__ = [
    "FORBIDDEN_HEADERS",
    "INTERNAL_PREFIX",
    "InProcessControlPlane",
    "LEASE_HEADER",
    "build_runner_router",
]
