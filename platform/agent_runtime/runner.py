"""AR-1 durable step runner and stale-run recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from agent_runtime.models import AgentRun
from claim_core import celery_app, new_ulid

STALE_AFTER = timedelta(minutes=15)
MAX_STEP_ATTEMPTS = 3
_runtime: dict[str, Any] = {}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


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

    def start(
        self,
        *,
        agent: str,
        capability_id: str,
        claim_id: str | None = None,
        trigger_event: str | None = None,
    ) -> str:
        """Create a durable run from the pack sequence without executing it."""

        declared = self.definitions.get(capability_id)
        if declared is None:
            raise ValueError(f"no COP steps declared for {capability_id!r}")
        run_id = new_ulid()
        now = self.app.state.clock()
        steps = [
            {
                "step_id": step_id,
                "status": "pending",
                "attempts": 0,
                "updated_at": now.isoformat(),
            }
            for step_id in declared
        ]
        with self.sessions.begin() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    agent=agent,
                    capability_id=capability_id,
                    claim_id=claim_id,
                    trigger_event=trigger_event,
                    status="running",
                    steps=steps,
                    autonomy_level=self.level(capability_id),
                    error=None,
                    started_at=now,
                    ended_at=None,
                )
            )
        return run_id

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

    def finish_deferred_run(
        self,
        run_id: str,
        *,
        status: str,
        outcome: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        """End a deferred run, recording the coarse stages it actually reached.

        ``outcome['stages']`` is an ordered list of ``{"id", "status", "ref"}``
        entries. Only stages already declared for the capability are recorded;
        an undeclared stage id is a defect, not something to invent a step for.
        """

        now = self.app.state.clock()
        stages = outcome.get("stages")
        recorded = {
            str(stage["id"]): stage
            for stage in (stages if isinstance(stages, list) else [])
            if isinstance(stage, dict) and isinstance(stage.get("id"), str)
        }
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            declared = {step.get("step_id") for step in run.steps}
            unknown = sorted(set(recorded) - declared)
            if unknown:
                raise ValueError(f"undeclared deferred stages {unknown}")
            steps = [dict(step) for step in run.steps]
            for step in steps:
                stage = recorded.get(str(step.get("step_id")))
                if stage is None:
                    continue
                step.update(
                    status=str(stage.get("status", "completed")),
                    attempts=max(1, int(step.get("attempts", 0))),
                    ended=now.isoformat(),
                    updated_at=now.isoformat(),
                    outcome={"ref": stage.get("ref")},
                )
            steps[-1].update(outcome={**steps[-1].get("outcome", {}), "result": dict(outcome)})
            run.steps = steps
            run.status = status
            run.error = error
            run.ended_at = now

    def heartbeat(self, run_id: str, step_id: str) -> None:
        """Persist a current-step heartbeat without relying on worker memory."""

        now = self.app.state.clock().isoformat()
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = [dict(step) for step in run.steps]
            for step in steps:
                if step.get("step_id") == step_id and step.get("status") == "running":
                    step["updated_at"] = now
                    run.steps = steps
                    return
            raise ValueError(f"step {step_id!r} is not running")

    def set_claim_id(self, run_id: str, claim_id: str) -> None:
        """Attach the claim created by a governed workflow step exactly once."""

        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            if run.claim_id not in {None, claim_id}:
                raise ValueError("agent run is already attached to a different claim")
            run.claim_id = claim_id

    @staticmethod
    def _next_step(run: AgentRun) -> tuple[int, dict[str, Any]] | None:
        for index, step in enumerate(run.steps):
            if step.get("status") != "completed":
                return index, dict(step)
        return None

    def run(self, run_id: str) -> dict[str, Any]:
        """Resume from the first incomplete persisted step and end on review."""

        while True:
            with self.sessions() as session:
                run = session.get(AgentRun, run_id)
                if run is None:
                    raise LookupError(f"agent run {run_id} was not found")
                session.expunge(run)
            if run.status != "running":
                return {"run_id": run.id, "status": run.status}
            pending = self._next_step(run)
            if pending is None:
                now = self.app.state.clock()
                with self.sessions.begin() as session:
                    current = session.get(AgentRun, run_id)
                    if current is not None:
                        current.status = "completed"
                        current.ended_at = now
                return {"run_id": run_id, "status": "completed"}
            index, step = pending
            step_id = str(step["step_id"])
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
            except Exception as error:  # noqa: BLE001 - reaper owns bounded recovery
                self._record_step_error(run_id, index, error)
                return {"run_id": run_id, "status": "running", "error": type(error).__name__}
            outcome = dict(raw) if isinstance(raw, dict) else {"result": raw}
            if outcome.get("status") == "waiting":
                expects_event = outcome.get("expects_event")
                if not isinstance(expects_event, str) or not expects_event:
                    self._record_step_error(
                        run_id,
                        index,
                        ValueError("waiting outcome requires expects_event"),
                    )
                    return {"run_id": run_id, "status": "running", "error": "ValueError"}
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
            review_id = outcome.get("review_id")
            awaits_review = outcome.get("status") in {"staged", "awaiting_review"} or isinstance(
                review_id, str
            )
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
            if awaits_review:
                return {"run_id": run_id, "status": "awaiting_review", "review_id": review_id}

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

    def consume(self, event: Any) -> None:
        """Resume runs after durable review resolution or a waited-for event."""

        if event.type != "review.resolved":
            self._resume_waiting(event)
            return
        run_id = event.payload.get("agent_run_id")
        review_payload: dict[str, Any] | None = None
        review_id = event.payload.get("review_id")
        if isinstance(review_id, str):
            with self.app.state.engine.connect() as connection:
                raw = connection.execute(
                    text("SELECT payload FROM review_items WHERE id = :id"),
                    {"id": review_id},
                ).scalar()
            payload = _json_value(raw)
            if isinstance(payload, dict):
                review_payload = payload
                if not isinstance(run_id, str):
                    run_id = payload.get("agent_run_id")
        if not isinstance(run_id, str):
            return
        action = review_payload.get("action") if review_payload is not None else None
        if (
            event.payload.get("resolution") == "rejected"
            and isinstance(action, dict)
            and action.get("type") == "intake.create_claim"
        ):
            now = self.app.state.clock()
            with self.sessions.begin() as session:
                run = session.get(AgentRun, run_id)
                if run is None or run.status != "awaiting_review":
                    return
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
        with self.sessions.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None or run.status != "awaiting_review":
                return
            run.status = "running"
            run.error = None
        self.run(run_id)

    def _resume_waiting(self, event: Any) -> None:
        """Re-invoke waiting steps whose declared event has now committed."""

        with self.sessions() as session:
            runs = list(session.scalars(select(AgentRun).where(AgentRun.status == "running")))
            for run in runs:
                session.expunge(run)
        for run in runs:
            if event.claim_id is not None and run.claim_id != event.claim_id:
                continue
            pending = self._next_step(run)
            if pending is None:
                continue
            _index, step = pending
            outcome = step.get("outcome")
            if (
                step.get("status") == "waiting"
                and isinstance(outcome, dict)
                and outcome.get("expects_event") == event.type
            ):
                self.run(run.id)

    def reap(self) -> int:
        """Resume stale running steps, failing visibly after three attempts."""

        now = _aware(self.app.state.clock())
        with self.sessions() as session:
            runs = list(session.scalars(select(AgentRun).where(AgentRun.status == "running")))
            for run in runs:
                session.expunge(run)
        reaped = 0
        for run in runs:
            pending = self._next_step(run)
            if pending is None:
                continue
            _index, step = pending
            raw_updated = step.get("updated_at")
            if not isinstance(raw_updated, str):
                continue
            updated = _aware(datetime.fromisoformat(raw_updated))
            if now - updated <= STALE_AFTER:
                continue
            reaped += 1
            if int(step.get("attempts", 0)) >= MAX_STEP_ATTEMPTS:
                self._fail_exhausted(run, str(step.get("step_id")))
            else:
                self.run(run.id)
        return reaped

    def _fail_exhausted(self, run: AgentRun, step_id: str) -> None:
        now = self.app.state.clock()
        error = {"code": "STEP_ATTEMPTS_EXHAUSTED", "step_id": step_id}
        with self.sessions.begin() as session:
            current = session.get(AgentRun, run.id)
            if current is None or current.status != "running":
                return
            current.status = "failed"
            current.error = error
            current.ended_at = now
            self.app.state.record_event(
                session,
                claim_id=current.claim_id,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "EXCEPTION",
                    "subtype": "agent_run_failed",
                    "agent_run_id": current.id,
                    "capability_id": current.capability_id,
                    "step_id": step_id,
                },
                actor="system",
                correlation_id=current.id,
            )


def configure_reaper(runner: AgentRunner) -> None:
    _runtime["runner"] = runner
    celery_app.conf.beat_schedule["agent-runtime-reaper"] = {
        "task": "agent_runtime.reap_stale_runs",
        "schedule": 300.0,
    }


@celery_app.task(name="agent_runtime.reap_stale_runs", acks_late=True)
def reap_stale_runs() -> int:
    runner = _runtime.get("runner")
    if runner is None:
        raise RuntimeError("agent runtime reaper is not configured")
    return runner.reap()


__all__ = ["AgentRunner", "StepContext", "configure_reaper", "reap_stale_runs"]
