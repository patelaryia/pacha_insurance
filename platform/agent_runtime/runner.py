"""AR-1 durable step runner and stale-run recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from agent_runtime.models import AgentRun
from claim_core import new_ulid
from claim_core.models import Event

#: Governed actions that are not Workflows still need an operational projection
#: row for AR-2 review and audit correlation. This prefix is deliberately not a
#: Temporal Workflow-ID kind and must never enter Workflow history.
DOMAIN_ACTION_ID_PREFIX = "pacha.domain.action."
DOMAIN_ACTION_TYPE = "GovernedDomainAction"
DOMAIN_ACTION_BUILD_ID = "domain-gate"


def domain_action_id(run_id: str) -> str:
    """Return the non-Workflow identity for one governed AR-2 action."""

    return f"{DOMAIN_ACTION_ID_PREFIX}{run_id}"


@dataclass(frozen=True)
class StepContext:
    """Stable input supplied to every idempotent agent step."""

    run_id: str
    claim_id: str | None
    capability_id: str
    step_id: str
    trigger_event: str | None = None


class AgentRunner:
    """Execute pack-declared steps, persisting progress before every boundary."""

    def __init__(self, app: Any, definitions_path: Path) -> None:
        self.app = app
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.definitions = self._load_definitions(definitions_path)
        self._steps: dict[tuple[str, str], Callable[[StepContext], Any]] = {}

    @staticmethod
    def _load_definitions(path: Path) -> dict[str, tuple[str, ...]]:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"invalid COP step definitions: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("COP step definitions require version 1")
        rows = payload.get("step_definitions")
        if not isinstance(rows, list):
            raise ValueError("COP step definitions must be a list")
        definitions: dict[str, tuple[str, ...]] = {}
        for row in rows:
            capability_id = row.get("capability_id") if isinstance(row, dict) else None
            steps = row.get("steps") if isinstance(row, dict) else None
            if (
                not isinstance(capability_id, str)
                or capability_id in definitions
                or not isinstance(steps, list)
            ):
                raise ValueError("invalid or duplicate COP step definition")
            ids = tuple(
                step.get("id") if isinstance(step, dict) else None for step in steps
            )
            if not ids or any(not isinstance(step_id, str) or not step_id for step_id in ids):
                raise ValueError(f"invalid steps for {capability_id}")
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate steps for {capability_id}")
            definitions[capability_id] = ids  # type: ignore[assignment]
        return definitions

    def register_step(
        self,
        capability_id: str,
        step_id: str,
        fn: Callable[[StepContext], Any],
    ) -> None:
        """Register one idempotent callable only for a declared step id."""

        if step_id not in self.definitions.get(capability_id, ()):
            raise ValueError(f"undeclared step {capability_id}:{step_id}")
        key = (capability_id, step_id)
        if key in self._steps or not callable(fn):
            raise ValueError(f"step {capability_id}:{step_id} is already registered or invalid")
        self._steps[key] = fn

    def level(self, capability_id: str) -> str:
        with self.app.state.engine.connect() as connection:
            level = connection.execute(
                text("SELECT current_level FROM capabilities WHERE id = :id"),
                {"id": capability_id},
            ).scalar()
        if not isinstance(level, str):
            raise ValueError(f"unknown capability {capability_id!r}")
        return level

    def record_action_start(
        self,
        *,
        agent: str,
        capability_id: str,
        claim_id: str | None,
        action_type: str,
        autonomy_level: str,
    ) -> str:
        """Record an execute_or_stage turn that is not a COP workflow run."""

        run_id = new_ulid()
        now = self.app.state.clock()
        with self.sessions.begin() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    agent=agent,
                    capability_id=capability_id,
                    claim_id=claim_id,
                    trigger_event=None,
                    workflow_id=domain_action_id(run_id),
                    workflow_run_id=None,
                    workflow_type=DOMAIN_ACTION_TYPE,
                    worker_build_id=DOMAIN_ACTION_BUILD_ID,
                    status="running",
                    steps=[
                        {
                            "step_id": "execute_or_stage",
                            "status": "running",
                            "attempts": 1,
                            "action_type": action_type,
                            "started": now.isoformat(),
                            "updated_at": now.isoformat(),
                        }
                    ],
                    autonomy_level=autonomy_level,
                    error=None,
                    last_workflow_event_ref=None,
                    last_synced_at=None,
                    started_at=now,
                    ended_at=None,
                )
            )
        return run_id

    def finish_action(
        self,
        run_id: str,
        *,
        status: str,
        outcome: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        """End one gate turn with a persisted outcome."""

        now = self.app.state.clock()
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = [dict(step) for step in run.steps]
            steps[-1].update(
                status="completed" if status == "completed" else status,
                ended=now.isoformat(),
                updated_at=now.isoformat(),
                outcome=dict(outcome),
            )
            run.steps = steps
            run.status = status
            run.error = error
            run.ended_at = now if status in {"completed", "failed", "blocked"} else None

    def set_claim_id(self, run_id: str, claim_id: str) -> None:
        """Attach the claim created by a governed workflow step exactly once."""

        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            if run.claim_id not in {None, claim_id}:
                raise ValueError("agent run is already attached to a different claim")
            run.claim_id = claim_id

    def apply_control_event(self, run_id: str, event_ref: str) -> None:
        """Apply one Temporal-delivered opaque control event to domain step state."""

        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            event = session.get(Event, event_ref)
            if run is None or event is None:
                raise LookupError("run or control event was not found")
            if event.type not in {
                "intake.document_ready",
                "intake.review_resolved",
                "intake.claim_terminal",
            }:
                raise ValueError("event is not an intake control event")
            if event.claim_id is not None and run.claim_id not in {None, event.claim_id}:
                raise ValueError("control event belongs to another claim")
            if event.type == "intake.review_resolved" and run.status == "awaiting_review":
                if (
                    event.payload.get("resolution") == "rejected"
                    and event.payload.get("action_type") == "intake.create_claim"
                ):
                    now = self.app.state.clock()
                    steps = [dict(step) for step in run.steps]
                    for step in steps:
                        if (
                            step.get("step_id") == "create_claim"
                            and step.get("status") == "completed"
                        ):
                            outcome = step.get("outcome")
                            step["outcome"] = {
                                **(outcome if isinstance(outcome, dict) else {}),
                                "resolution": "rejected",
                                "result": "no_op",
                            }
                        elif step.get("status") == "completed":
                            continue
                        else:
                            step.update(
                                status="completed",
                                ended=now.isoformat(),
                                updated_at=now.isoformat(),
                                outcome={
                                    "status": "skipped",
                                    "reason": "claim_creation_rejected",
                                },
                            )
                    run.steps = steps
                    run.status = "completed"
                    run.error = None
                    run.ended_at = now
                    return
                run.status = "running"
                run.error = None
            elif event.type == "intake.claim_terminal":
                run.status = "cancelled"
                run.ended_at = self.app.state.clock()

    @staticmethod
    def _next_step(run: AgentRun) -> tuple[int, dict[str, Any]] | None:
        for index, step in enumerate(run.steps):
            if step.get("status") != "completed":
                return index, dict(step)
        return None

    def execute_activity_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        """Execute exactly the persisted step requested by a Temporal Activity."""

        with self.sessions() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            session.expunge(run)
        if run.status != "running":
            return {"run_id": run.id, "status": run.status}
        pending = self._next_step(run)
        if pending is None:
            return self._complete_projection(run_id)
        index, step = pending
        if step.get("step_id") != step_id:
            raise ValueError(f"step {step_id!r} is not the current persisted step")
        fn = self._steps.get((run.capability_id, step_id))
        if fn is None:
            return self._block_missing_step(run, index, step_id)

        now = self.app.state.clock()
        attempts = int(step.get("attempts", 0)) + 1
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run_id)
            if current is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = [dict(item) for item in current.steps]
            steps[index].update(
                status="running",
                attempts=attempts,
                started=steps[index].get("started", now.isoformat()),
                updated_at=now.isoformat(),
            )
            current.steps = steps
        context = StepContext(
            run_id,
            run.claim_id,
            run.capability_id,
            step_id,
            run.trigger_event,
        )
        try:
            raw = fn(context)
        except Exception as error:  # noqa: BLE001 - record then let Temporal retry
            self._record_step_error(run_id, index, error)
            raise
        outcome = dict(raw) if isinstance(raw, dict) else {"result": raw}
        if outcome.get("status") == "waiting":
            return self._record_waiting(run_id, index, attempts, outcome)

        review_id = outcome.get("review_id")
        awaits_review = outcome.get("status") in {
            "staged",
            "awaiting_review",
        } or isinstance(review_id, str)
        ended = self.app.state.clock()
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run_id)
            if current is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = [dict(item) for item in current.steps]
            steps[index].update(
                status=(
                    "awaiting_review"
                    if awaits_review and outcome.get("resume_step") is True
                    else "completed"
                ),
                ended=ended.isoformat(),
                updated_at=ended.isoformat(),
                outcome=outcome,
            )
            current.steps = steps
            current.error = None
            if awaits_review:
                current.status = "awaiting_review"
            elif all(item.get("status") == "completed" for item in steps):
                current.status = "completed"
                current.ended_at = ended
        if awaits_review:
            return {
                "run_id": run_id,
                "status": "awaiting_review",
                "review_id": review_id,
            }
        return {
            "run_id": run_id,
            "status": "completed" if index == len(run.steps) - 1 else "running",
        }

    def _complete_projection(self, run_id: str) -> dict[str, Any]:
        now = self.app.state.clock()
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run_id)
            if current is not None:
                current.status = "completed"
                current.ended_at = now
        return {"run_id": run_id, "status": "completed"}

    def _record_waiting(
        self,
        run_id: str,
        index: int,
        attempts: int,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        expects_event = outcome.get("expects_event")
        if not isinstance(expects_event, str) or not expects_event:
            error = ValueError("waiting outcome requires expects_event")
            self._record_step_error(run_id, index, error)
            raise error
        waited_at = self.app.state.clock()
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run_id)
            if current is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = [dict(item) for item in current.steps]
            steps[index].update(
                status="waiting",
                attempts=max(0, attempts - 1),
                updated_at=waited_at.isoformat(),
                outcome=outcome,
            )
            current.steps = steps
            current.error = None
        return {
            "run_id": run_id,
            "status": "running",
            "expects_event": expects_event,
        }

    def _block_missing_step(
        self, run: AgentRun, index: int, step_id: str
    ) -> dict[str, Any]:
        error = {"code": "STEP_NOT_REGISTERED", "step_id": step_id}
        now = self.app.state.clock()
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run.id)
            if current is not None:
                steps = [dict(item) for item in current.steps]
                steps[index].update(status="blocked", outcome=error, updated_at=now.isoformat())
                current.steps = steps
                current.status = "blocked"
                current.error = error
                current.ended_at = now
        return {"run_id": run.id, "status": "blocked", "error": error}

    def _record_step_error(self, run_id: str, index: int, error: Exception) -> None:
        now = self.app.state.clock()
        detail = {"type": type(error).__name__, "message": str(error)[:1000]}
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                return
            steps = [dict(item) for item in run.steps]
            steps[index].update(status="running", updated_at=now.isoformat(), outcome=detail)
            run.steps = steps
            run.error = detail

__all__ = [
    "DOMAIN_ACTION_BUILD_ID",
    "DOMAIN_ACTION_ID_PREFIX",
    "DOMAIN_ACTION_TYPE",
    "AgentRunner",
    "StepContext",
    "domain_action_id",
]
