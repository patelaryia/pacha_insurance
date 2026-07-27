"""The `agent_runs` Temporal projection service (master plan §13).

Three narrow entry points, deliberately shaped by *who owns the transaction*:

* `prepare` runs inside the caller's transaction, because the pending row and
  the initiating Pacha event must commit together or not at all. If the domain
  transaction rolls back there must be no orphan run claiming a Workflow
  identity that was never started.
* `record_started` and `record_status` own their transactions, because they are
  called from Activities after Temporal has already made the corresponding fact
  durable in Workflow history.

Nothing here is claim truth. `steps` and `error` detail belong to the
domain-specific Activities of T03–T06, which write them after their own Pacha
commits; `record_status` deliberately refuses to touch either, so a generic
status sync can never overwrite a step outcome it knows nothing about.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.models import AgentRun
from orchestration.contracts import ControlResult, validate_control_field
from orchestration.ids import WorkflowRef

__all__ = [
    "AgentRunConflict",
    "AgentRunNotFound",
    "AgentRunProjection",
]


class AgentRunNotFound(LookupError):
    """No `agent_runs` row exists for the supplied run reference."""


class AgentRunConflict(ValueError):
    """The observation contradicts the row's recorded Workflow or lifecycle."""


#: Statuses whose Workflow is still executing, so a new Run ID is legitimate.
_ACTIVE_STATUSES: frozenset[str] = frozenset({"running", "awaiting_review"})

#: Terminal statuses. A row that reaches one never leaves or changes it.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"blocked", "completed", "failed", "cancelled"}
)

#: The complete legal transition table. Same-to-same is idempotent everywhere
#: and is handled before this map is consulted.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset(
        {"awaiting_review", "blocked", "completed", "failed", "cancelled"}
    ),
    "awaiting_review": frozenset(
        {"running", "blocked", "completed", "failed", "cancelled"}
    ),
}

#: Code-owned identifiers: an agent name, a capability id, a Workflow type name
#: or a Worker build id. No whitespace, so no prose or claim fact fits.
_CODE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$")


def _require_code_token(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _CODE_TOKEN_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a non-empty code-owned identifier")
    return value


class AgentRunProjection:
    """Create and synchronise the operational projection of a Temporal run."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    # -- creation, inside the caller's transaction ----------------------------

    def prepare(
        self,
        session: Session,
        *,
        run_ref: str,
        agent: str,
        capability_id: str,
        autonomy_level: str,
        workflow_ref: WorkflowRef,
        workflow_type: str,
        claim_ref: str | None = None,
        trigger_event_ref: str | None = None,
        step_ids: Sequence[str] = (),
    ) -> None:
        """Insert the `pending` row in the caller's open transaction.

        Flushes so a unique or foreign-key violation surfaces inside that
        transaction, and never commits: the caller decides whether the run and
        the event that justifies it become durable together.

        Raises:
            ValueError: an opaque reference, identifier or step id is invalid.
        """

        validate_control_field("run_ref", run_ref)
        if not isinstance(workflow_ref, WorkflowRef):
            raise ValueError("workflow_ref must be an orchestration.ids.WorkflowRef")
        _require_code_token("agent", agent)
        _require_code_token("capability_id", capability_id)
        _require_code_token("workflow_type", workflow_type)
        _require_code_token("autonomy_level", autonomy_level)
        if claim_ref is not None:
            validate_control_field("claim_ref", claim_ref)
        if trigger_event_ref is not None:
            validate_control_field("trigger_event_ref", trigger_event_ref)

        seen: set[str] = set()
        for step_id in step_ids:
            validate_control_field("step_id", step_id)
            if step_id in seen:
                raise ValueError(f"duplicate step id {step_id!r} in the declared sequence")
            seen.add(step_id)

        now = self._app.state.clock()
        session.add(
            AgentRun(
                id=run_ref,
                agent=agent,
                capability_id=capability_id,
                claim_id=claim_ref,
                trigger_event=trigger_event_ref,
                workflow_id=workflow_ref.workflow_ref,
                workflow_run_id=None,
                workflow_type=workflow_type,
                worker_build_id=None,
                status="pending",
                steps=[
                    {
                        "step_id": step_id,
                        "status": "pending",
                        "attempts": 0,
                        "updated_at": now.isoformat(),
                    }
                    for step_id in step_ids
                ],
                autonomy_level=autonomy_level,
                error=None,
                last_workflow_event_ref=None,
                last_synced_at=None,
                started_at=now,
                ended_at=None,
            )
        )
        session.flush()

    # -- synchronisation, owning its own transaction --------------------------

    def record_started(
        self,
        *,
        run_ref: str,
        workflow_ref: str,
        workflow_run_ref: str,
        workflow_type: str,
        worker_build_id: str,
    ) -> None:
        """Record the Workflow Run ID and build the execution is actually on.

        The Workflow ID and type are verified rather than trusted: the caller
        reads them from `activity.info()`, so a mismatch means the projection
        row and the execution have diverged, which is a conflict rather than
        something to overwrite.

        Raises:
            AgentRunNotFound: no row for `run_ref`.
            AgentRunConflict: identity mismatch, or the row is already terminal.
        """

        validate_control_field("run_ref", run_ref)
        validate_control_field("workflow_ref", workflow_ref)
        validate_control_field("workflow_run_ref", workflow_run_ref)
        _require_code_token("workflow_type", workflow_type)
        _require_code_token("worker_build_id", worker_build_id)

        now = self._app.state.clock()
        with self._sessions.begin() as session:
            run = self._locked(session, run_ref)
            if run.workflow_id != workflow_ref:
                raise AgentRunConflict(
                    f"agent run {run_ref} is projected onto a different Workflow ID"
                )
            if run.workflow_type != workflow_type:
                raise AgentRunConflict(
                    f"agent run {run_ref} is projected onto a different Workflow type"
                )
            if run.status in _TERMINAL_STATUSES:
                raise AgentRunConflict(
                    f"agent run {run_ref} is terminal ({run.status}) and cannot be restarted"
                )
            if run.status == "pending":
                run.status = "running"
            # Active rows accept a changed Run ID: Continue-As-New mints a new
            # one for the same Workflow ID, and refusing it would strand the
            # projection on the previous run.
            run.workflow_run_id = workflow_run_ref
            run.worker_build_id = worker_build_id
            run.last_synced_at = now

    def record_status(self, result: ControlResult) -> None:
        """Apply one control-only status observation to the projection.

        Deliberately generic and deliberately narrow: it moves `status`,
        `ended_at`, `last_workflow_event_ref` and `last_synced_at` and nothing
        else. `steps` and `error` carry domain detail this method cannot
        reconstruct, so it leaves both exactly as the owning Activity wrote them.

        Raises:
            AgentRunNotFound: no row for the result's run reference.
            AgentRunConflict: the transition is not in the legal table.
        """

        run_ref = result.run_ref
        if run_ref is None:
            raise ValueError("record_status requires a ControlResult carrying run_ref")
        status = result.status

        now = self._app.state.clock()
        with self._sessions.begin() as session:
            run = self._locked(session, run_ref)
            current = run.status
            if status != current:
                if status not in _TRANSITIONS.get(current, frozenset()):
                    raise AgentRunConflict(
                        f"agent run {run_ref} cannot move from {current} to {status}"
                    )
                run.status = status

            if status in _TERMINAL_STATUSES:
                # Idempotent repeat keeps the original end time.
                if run.ended_at is None:
                    run.ended_at = now
            else:
                run.ended_at = None

            # A review reference is the more specific observation when a result
            # carries both, so it wins.
            event_ref = result.review_event_ref or result.event_ref
            if event_ref is not None:
                run.last_workflow_event_ref = event_ref
            run.last_synced_at = now

    # -- internals ------------------------------------------------------------

    def _locked(self, session: Session, run_ref: str) -> AgentRun:
        """Load one row, holding a row lock where the dialect provides one."""

        query = select(AgentRun).where(AgentRun.id == run_ref)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        run = session.scalar(query)
        if run is None:
            raise AgentRunNotFound(f"agent run {run_ref} was not found")
        return run
