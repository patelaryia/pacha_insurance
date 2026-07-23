"""Public AR-1/AR-2/AR-3 runtime boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_runtime.comms import CommunicationsService
from agent_runtime.gate import (
    Action,
    AutonomyGate,
    DeferredAction,
    WorkReceipt,
    execute_authorised_adapter,
    load_gate_config,
)
from agent_runtime.models import AgentRun
from agent_runtime.runner import AgentRunner, StepContext, configure_reaper
from claim_core import Base


class AgentRuntime:
    """Application-owned facade for governed actions and durable COP runs."""

    def __init__(self, app: Any, *, grade: Any = None) -> None:
        if not all(
            hasattr(app.state, dependency)
            for dependency in ("cop_runtime", "eval_harness", "review_queue")
        ):
            raise RuntimeError(
                "build_agent_runtime requires COP runtime, eval harness, and review queue"
            )
        repo = Path(__file__).resolve().parents[2]
        pack_root = repo / "packs" / "motor"
        Base.metadata.create_all(app.state.engine, tables=[AgentRun.__table__])
        self.app = app
        app.state.eval_harness.graders.activate_gcomm()
        self.runner = AgentRunner(app, pack_root / "cop_steps.yaml")
        self.gate = AutonomyGate(
            app,
            self.runner,
            load_gate_config(pack_root / "agent_runtime" / "gate.yaml"),
            grade=grade,
        )
        self.comms = CommunicationsService(app, self.gate, pack_root)

    def register_executor(self, action_type: str, fn: Any) -> None:
        self.gate.register_executor(action_type, fn)

    def execute_or_stage(
        self,
        *,
        capability_id: str,
        action: Action,
        claim_id: str | None,
        actor: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return self.gate.execute_or_stage(
            capability_id=capability_id,
            action=action,
            claim_id=claim_id,
            actor=actor,
            run_id=run_id,
        )

    def register_deferred_executor(self, action_type: str, fn: Any) -> None:
        """Register the curated PACKET-21 deferred contract (register #278)."""

        self.gate.register_deferred_executor(action_type, fn)

    def execute_staged(self, action: Action, *, run_id: str | None = None) -> Any:
        return self.gate.execute_staged(action, run_id=run_id)

    def finish_deferred(
        self,
        run_id: str,
        *,
        status: str,
        outcome: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        self.gate.finish_deferred(run_id, status=status, outcome=outcome, error=error)

    def register_step(self, capability_id: str, step_id: str, fn: Any) -> None:
        self.runner.register_step(capability_id, step_id, fn)

    def start_run(
        self,
        *,
        agent: str,
        capability_id: str,
        claim_id: str | None = None,
        trigger_event: str | None = None,
    ) -> str:
        return self.runner.start(
            agent=agent,
            capability_id=capability_id,
            claim_id=claim_id,
            trigger_event=trigger_event,
        )

    def run(self, run_id: str) -> dict[str, Any]:
        return self.runner.run(run_id)

    def reap(self) -> int:
        return self.runner.reap()


def build_agent_runtime(app: Any, *, grade: Any = None) -> AgentRuntime:
    """Build and wire the durable runtime after its three Phase-1 dependencies."""

    runtime = AgentRuntime(app, grade=grade)
    app.state.agent_runtime = runtime
    app.state.dispatcher.register_consumer("agent_runtime", runtime.runner.consume)
    configure_reaper(runtime.runner)
    return runtime


__all__ = [
    "Action",
    "AgentRuntime",
    "DeferredAction",
    "StepContext",
    "WorkReceipt",
    "build_agent_runtime",
    "execute_authorised_adapter",
]
