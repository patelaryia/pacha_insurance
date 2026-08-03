"""Public AR-1/AR-2/AR-3 runtime boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_runtime.comms import CommunicationsService
from agent_runtime.gate import Action, AutonomyGate, load_gate_config
from agent_runtime.models import AgentRun
from agent_runtime.projection import (
    AgentRunConflict,
    AgentRunNotFound,
    AgentRunProjection,
)
from agent_runtime.runner import AgentRunner
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
        self.projection = AgentRunProjection(app)
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

    def execute_staged(self, action: Action) -> Any:
        return self.gate.execute_staged(action)

    def register_step(self, capability_id: str, step_id: str, fn: Any) -> None:
        self.runner.register_step(capability_id, step_id, fn)

    def attach_claim_projection(self, run_id: str, claim_id: str) -> None:
        """Attach S1's committed claim to its pre-existing run projection."""

        self.runner.set_claim_id(run_id, claim_id)

    def apply_control_event(self, run_id: str, event_ref: str) -> None:
        """Apply one event reference delivered by the owning Temporal Workflow."""

        self.runner.apply_control_event(run_id, event_ref)

    def run_cop_activity(self, run_id: str, step_id: str) -> dict[str, Any]:
        """Execute no more than one named idempotent COP step."""

        with self.app.state.engine.begin() as connection:
            connection.execute(
                AgentRun.__table__.update()
                .where(AgentRun.id == run_id, AgentRun.status == "pending")
                .values(status="running")
            )
        return self.runner.execute_activity_step(run_id, step_id)

def build_agent_runtime(app: Any, *, grade: Any = None) -> AgentRuntime:
    """Build and wire the durable runtime after its three Phase-1 dependencies."""

    runtime = AgentRuntime(app, grade=grade)
    app.state.agent_runtime = runtime
    return runtime


__all__ = [
    "Action",
    "AgentRunConflict",
    "AgentRunNotFound",
    "AgentRunProjection",
    "AgentRuntime",
    "build_agent_runtime",
]
