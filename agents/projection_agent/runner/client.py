"""The outbound queue client. It opens no socket to listen on.

One cycle is: claim → execute → upload evidence → post result. Every call is an
outbound HTTPS request the runner initiates; the platform never calls in. The
raw lease token arrives once in the claim response, is held in memory for the
life of the attempt, and is presented on every callback.

`Adapter.execute` is never called from here directly: the runner goes through
``agent_runtime.gate.execute_authorised_adapter``, which refuses anything but a
live, matching work receipt (AR-2 / PACKET-21 §3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_runtime import WorkReceipt, execute_authorised_adapter
from projection_agent.adapters import OpResult
from projection_agent.runner.browser import StepFrame


class ControlPlane(Protocol):
    """The authenticated platform control API, as the runner sees it."""

    def claim(self, *, runner_id: str, systems: tuple[str, ...]) -> dict[str, Any] | None: ...

    def heartbeat(self, projection_id: str, *, token: str, runner_id: str) -> dict[str, Any]: ...

    def upload_evidence(
        self,
        projection_id: str,
        step_id: str,
        *,
        token: str,
        runner_id: str,
        phase: str,
        content: bytes,
    ) -> dict[str, Any]: ...

    def post_result(
        self, projection_id: str, *, token: str, runner_id: str, result: dict[str, Any]
    ) -> dict[str, Any]: ...

    def runner_heartbeat(self, *, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class RunnerClient:
    """One runner process. Stateless between cycles; nothing durable is kept."""

    runner_id: str
    systems: tuple[str, ...]
    control_plane: ControlPlane
    adapter_factory: Any
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    runner_version: str = "packet-21"

    def announce(self, *, browser_version: str, health: str = "healthy") -> dict[str, Any]:
        """Report ids, versions, clocks, and closed health codes. Nothing else."""

        return self.control_plane.runner_heartbeat(
            runner_id=self.runner_id,
            payload={
                "runner_version": self.runner_version,
                "browser_version": browser_version,
                "systems": list(self.systems),
                "health": health,
            },
        )

    def run_once(self) -> dict[str, Any] | None:
        """Claim at most one job and carry it to a posted result."""

        job = self.control_plane.claim(runner_id=self.runner_id, systems=self.systems)
        if job is None:
            return None
        token = job["lease_token"]
        projection_id = job["projection_id"]
        receipt = WorkReceipt(
            run_id=job["run_id"],
            projection_id=projection_id,
            operation=job["operation"],
            definition_version=job["definition_version"],
            attempt=int(job["attempt"]),
            expires_at=datetime.fromisoformat(job["expires_at"]),
            # Derived locally from the token the runner already holds; the
            # platform never returns a second copy of lease material.
            lease_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )
        uploaded: list[str] = []

        def on_frame(frame: StepFrame) -> None:
            recorded = self.control_plane.upload_evidence(
                projection_id,
                frame.step_id,
                token=token,
                runner_id=self.runner_id,
                phase=frame.phase,
                content=frame.content,
            )
            uploaded.append(str(recorded["evidence_id"]))

        def heartbeat() -> None:
            self.control_plane.heartbeat(
                projection_id, token=token, runner_id=self.runner_id
            )

        adapter = self.adapter_factory(job, on_frame=on_frame, heartbeat=heartbeat)
        # Revalidate the authoritative platform lease immediately before the
        # sole lawful Adapter.execute call. The receipt alone is not a bearer
        # credential and cannot prove that its lease remains current.
        heartbeat()
        result = execute_authorised_adapter(
            adapter,
            receipt=receipt,
            op=job["operation"],
            payload={"values": job["payload"], "claim_id": job["claim_id"]},
            run_id=job["run_id"],
            now=self.clock(),
            lease_token=token,
        )
        if not isinstance(result, OpResult):  # pragma: no cover - contract guard
            raise TypeError("an adapter must return the closed OpResult contract")
        posted = OpResult(
            outcome=result.outcome,
            last_completed_step=result.last_completed_step,
            write_ids=result.write_ids,
            readback_keys=result.readback_keys,
            evidence_ids=tuple(uploaded),
            reason_code=result.reason_code,
        )
        return self.control_plane.post_result(
            projection_id,
            token=token,
            runner_id=self.runner_id,
            result=posted.as_dict(),
        )


__all__ = ["ControlPlane", "RunnerClient"]
