"""The fixture runner identity and adapter factory — tests only.

`FixtureRunnerAuthenticator` exists **only** here. Production runner identity is
PACKET-22/infra work; with no authenticator injected the internal routes are not
mounted at all.
"""

from __future__ import annotations

from typing import Any

from claim_core import ClaimCoreError
from projection_agent.config import parse_click_path
from projection_agent.runner.target_adapters import TargetAdapter

FIXTURE_RUNNER_ID = "runner-fixture"
FIXTURE_HEADER = "x-runner-identity"


class FixtureRunnerAuthenticator:
    """A deterministic machine identity. It accepts no user token whatsoever."""

    def __init__(self, runner_id: str = FIXTURE_RUNNER_ID, secret: str = "fixture-secret") -> None:
        self.runner_id = runner_id
        self.secret = secret

    def authenticate(self, headers: dict[str, str]) -> str:
        presented = headers.get(FIXTURE_HEADER)
        if presented != self.secret:
            raise ClaimCoreError(
                403, "RUNNER_IDENTITY_INVALID", "The runner machine identity is not recognised"
            )
        return self.runner_id


def build_fixture_adapter(
    system: str,
    *,
    target: Any,
    timeouts: Any,
    clock: Any,
    runner_id: str = FIXTURE_RUNNER_ID,
    probe: Any = None,
    on_frame: Any = None,
    heartbeat: Any = None,
) -> TargetAdapter:
    """Construct one adapter bound to the synthetic target."""

    return TargetAdapter(
        system,  # type: ignore[arg-type]
        session_factory=target.session,
        timeouts=timeouts,
        clock=clock,
        runner_id=runner_id,
        probe=probe,
        on_frame=on_frame,
        heartbeat=heartbeat,
    )


def register_definition(adapter: TargetAdapter, definition: dict[str, Any]) -> None:
    """Parse the definition the control API returned, through the same loader."""

    adapter.register(
        parse_click_path(
            definition,
            operation_id=definition["operation"],
            version=definition["version"],
            executable=True,
        )
    )


__all__ = [
    "FIXTURE_HEADER",
    "FIXTURE_RUNNER_ID",
    "FixtureRunnerAuthenticator",
    "build_fixture_adapter",
    "register_definition",
]
