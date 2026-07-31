"""T02 unit suite — projection, outbox bridge and finite system Workflows.

Fakes are used here deliberately and narrowly: a fake dispatcher proves an
Activity calls the service it is specified to call and no other, a fake starter
proves a mapping constructs exactly the four opaque references it is allowed to.
Nothing that needs a real Workflow engine is faked — `test_temporal_t02.py` in
`tests/integration/` runs all of that against the real test server.

The migration cases carry `schema_isolated`, which is the repository's existing
tier policy: they get a private empty database, and `support.tiers` promotes
them into the required PostgreSQL tier. They are not optional runtime skips.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import warnings
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.models import AGENT_RUN_STATUSES, AgentRun
from agent_runtime.projection import (
    AgentRunConflict,
    AgentRunNotFound,
    AgentRunProjection,
)
from agent_runtime.runner import (
    LEGACY_WORKER_BUILD_ID,
    LEGACY_WORKFLOW_TYPE,
    legacy_workflow_id,
)
from claim_core import Base
from claim_core.app import create_app
from claim_core.ledger import LEDGER_ADVISORY_LOCK_KEY, LEDGER_ADVISORY_LOCK_SQL
from claim_core.models import Event
from claim_core.outbox import MAX_DISPATCH_LIMIT
from orchestration.activities import (
    DISPATCH_BATCH_SIZE,
    AgentRunActivities,
    SystemActivities,
    control_activity_registrations,
    ledger_activity_registrations,
)
from orchestration.contracts import (
    REQUIRED_ID_CONFLICT_POLICY,
    REQUIRED_ID_REUSE_POLICY,
    ControlCommand,
    ControlResult,
    ControlSignal,
)
from orchestration.errors import ControlContractError
from orchestration.ids import agent_workflow_ref, chase_workflow_ref
from orchestration.starter import (
    STANDARD_SIGNAL_NAMES,
    TEMPORAL_INTENT_MAPPINGS,
    TemporalIntentConsumer,
    TemporalIntentMapping,
    TemporalStarter,
)
from orchestration.workflows import MAX_DRAIN_BATCHES, SYSTEM_WORKFLOWS
from support.temporal import local_config

REPO = pathlib.Path(__file__).resolve().parents[2]

RUN_A = "01JZ8QA1B2C3D4E5F6G7H8J9K0"
RUN_B = "01JZ8QB1B2C3D4E5F6G7H8J9K0"
CLAIM_A = "01JZ8QC1B2C3D4E5F6G7H8J9K0"
EVENT_A = "01JZ8QD1B2C3D4E5F6G7H8J9K0"
EVENT_B = "01JZ8QE1B2C3D4E5F6G7H8J9K0"
REVIEW_A = "01JZ8QF1B2C3D4E5F6G7H8J9K0"
WORKFLOW_RUN_A = "6f8b2c1d-4e5a-4b7c-9d0e-1f2a3b4c5d6e"
WORKFLOW_RUN_B = "7a9c3d2e-5f6b-4c8d-8e1f-2a3b4c5d6e7f"


def _database_url(tmp_path: pathlib.Path, name: str) -> str:
    return os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")


def _subprocess_stdout(program: str) -> str:
    """Run `program` in a clean interpreter on the repository's import path.

    Import-surface assertions cannot be made in this process: the suite has
    already imported the very modules they are checking are absent.
    """

    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "platform"), str(REPO / "agents"), str(REPO / "packs"), str(REPO / "tests")]
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return completed.stdout.strip()


def _alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(REPO / "platform" / "claim_core" / "alembic")
    )
    config.set_main_option("sqlalchemy.url", url)
    return config


# =============================================================================
# 1. the exact `agent_runs` model
# =============================================================================

EXPECTED_COLUMNS = (
    "id",
    "agent",
    "capability_id",
    "claim_id",
    "trigger_event",
    "workflow_id",
    "workflow_run_id",
    "workflow_type",
    "worker_build_id",
    "status",
    "steps",
    "autonomy_level",
    "error",
    "last_workflow_event_ref",
    "last_synced_at",
    "started_at",
    "ended_at",
)


def test_the_model_declares_the_exact_section_0_5_column_set_in_order():
    assert tuple(column.name for column in AgentRun.__table__.columns) == EXPECTED_COLUMNS


def test_identity_columns_carry_the_exact_nullability_the_ddl_states():
    columns = AgentRun.__table__.columns
    assert columns["workflow_id"].nullable is False
    assert columns["workflow_type"].nullable is False
    # Run ID changes on Continue-As-New and build ID is unknown until an
    # Activity observes it, so both must stay nullable.
    for optional in ("workflow_run_id", "worker_build_id", "last_workflow_event_ref"):
        assert columns[optional].nullable is True
    assert columns["last_synced_at"].type.timezone is True
    assert columns["started_at"].type.timezone is True


def test_no_foreign_key_is_declared_from_claim_id():
    assert AgentRun.__table__.columns["claim_id"].foreign_keys == set()
    assert {
        key.column.table.name for key in AgentRun.__table__.columns["trigger_event"].foreign_keys
    } == {"events"}


def test_the_status_check_holds_the_seven_values_under_its_original_name():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in AgentRun.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert set(checks) == {"ck_agent_runs_status"}
    body = checks["ck_agent_runs_status"]
    assert set(AGENT_RUN_STATUSES) == {
        "pending",
        "running",
        "awaiting_review",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    }
    for status in AGENT_RUN_STATUSES:
        assert f"'{status}'" in body


def test_the_unique_constraint_and_both_indexes_are_declared_with_exact_names():
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in AgentRun.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique == {"uq_agent_runs_workflow_id": ("workflow_id",)}
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in AgentRun.__table__.indexes
    }
    assert indexes == {
        "ix_agent_runs_status": ("status",),
        "ix_agent_runs_claim": ("claim_id",),
    }


def test_no_lease_attempt_schedule_or_timestamp_column_was_invented():
    forbidden = {"created_at", "updated_at", "attempts", "lease", "schedule_id", "payload"}
    assert forbidden.isdisjoint({column.name for column in AgentRun.__table__.columns})


# =============================================================================
# 2. legacy-runner compatibility
# =============================================================================


@pytest.fixture()
def runtime_app(tmp_path):
    """A real application with `agent_runs` present and the T02 clock injected."""

    app = create_app(_database_url(tmp_path, "t02"))
    Base.metadata.create_all(app.state.engine, tables=[AgentRun.__table__])
    return app


def _seed_run(app, **overrides: Any) -> str:
    values = {
        "id": RUN_A,
        "agent": "agent:intake",
        "capability_id": "intake.triage",
        "claim_id": None,
        "trigger_event": None,
        "workflow_id": str(agent_workflow_ref(RUN_A)),
        "workflow_run_id": None,
        "workflow_type": "PachaTestReviewWaitWorkflow",
        "worker_build_id": None,
        "status": "pending",
        "steps": [],
        "autonomy_level": "L1",
        "error": None,
        "last_workflow_event_ref": None,
        "last_synced_at": None,
        "started_at": app.state.clock(),
        "ended_at": None,
    }
    values.update(overrides)
    with Session(app.state.engine) as session, session.begin():
        session.add(AgentRun(**values))
    return values["id"]


def test_every_legacy_runner_row_receives_the_four_legacy_metadata_values(tmp_path):
    from agent_runtime.runner import AgentRunner
    from eval_harness.models import Capability

    app = create_app(_database_url(tmp_path, "legacy"))
    Base.metadata.create_all(
        app.state.engine, tables=[AgentRun.__table__, Capability.__table__]
    )
    with Session(app.state.engine) as session, session.begin():
        session.add(
            Capability(
                id="intake.claim_creation", current_level="L1", max_level="L2", policy={}
            )
        )
    runner = AgentRunner(app, REPO / "packs" / "motor" / "cop_steps.yaml")

    workflow_run = runner.start(agent="agent:intake", capability_id="intake.claim_creation")
    action_run = runner.record_action_start(
        agent="agent:intake",
        capability_id="intake.claim_creation",
        claim_id=None,
        action_type="notify.send",
        autonomy_level="L1",
    )

    with Session(app.state.engine) as session:
        for run_id in (workflow_run, action_run):
            row = session.get(AgentRun, run_id)
            assert row.workflow_id == legacy_workflow_id(run_id)
            assert row.workflow_type == LEGACY_WORKFLOW_TYPE
            assert row.worker_build_id == LEGACY_WORKER_BUILD_ID
            assert row.workflow_run_id is None
            assert row.last_workflow_event_ref is None
            assert row.last_synced_at is None
            # Unchanged: `pending` belongs to the business migration packets.
            assert row.status == "running"


def test_the_legacy_workflow_id_is_refused_by_the_control_contract():
    """It is migration metadata, so it must not be usable as a Workflow ID."""

    from orchestration.contracts import validate_control_field

    with pytest.raises(ControlContractError):
        validate_control_field("workflow_ref", legacy_workflow_id(RUN_A))


# =============================================================================
# 3-4. the projection service
# =============================================================================


def test_prepare_joins_the_callers_transaction_and_a_rollback_leaves_no_row(runtime_app):
    projection = AgentRunProjection(runtime_app)
    sessions = sessionmaker(bind=runtime_app.state.engine, expire_on_commit=False)

    session = sessions()
    try:
        projection.prepare(
            session,
            run_ref=RUN_A,
            agent="agent:chase",
            capability_id="chase.request",
            autonomy_level="L2",
            workflow_ref=chase_workflow_ref(CLAIM_A),
            workflow_type="DocumentChaseWorkflow",
            step_ids=("ingest", "populate"),
        )
        # Visible inside the transaction: it flushed rather than committed.
        assert session.scalar(select(AgentRun.status).where(AgentRun.id == RUN_A)) == "pending"
        session.rollback()
    finally:
        session.close()

    with Session(runtime_app.state.engine) as check:
        assert check.get(AgentRun, RUN_A) is None


def test_prepare_writes_the_declared_pending_shape(runtime_app):
    projection = AgentRunProjection(runtime_app)
    with Session(runtime_app.state.engine) as session, session.begin():
        projection.prepare(
            session,
            run_ref=RUN_A,
            agent="agent:chase",
            capability_id="chase.request",
            autonomy_level="L2",
            workflow_ref=chase_workflow_ref(CLAIM_A),
            workflow_type="DocumentChaseWorkflow",
            claim_ref=CLAIM_A,
            step_ids=("ingest", "populate"),
        )

    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    assert row.status == "pending"
    assert row.workflow_id == str(chase_workflow_ref(CLAIM_A))
    assert row.autonomy_level == "L2"
    assert [step["step_id"] for step in row.steps] == ["ingest", "populate"]
    assert all(step["status"] == "pending" and step["attempts"] == 0 for step in row.steps)
    assert row.workflow_run_id is None
    assert row.worker_build_id is None
    assert row.last_synced_at is None
    assert row.last_workflow_event_ref is None
    assert row.error is None
    assert row.ended_at is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"run_ref": "not-a-ulid"}, "run_ref"),
        ({"agent": ""}, "agent"),
        ({"capability_id": "a capability"}, "capability_id"),
        ({"workflow_type": ""}, "workflow_type"),
        ({"claim_ref": "nope"}, "claim_ref"),
        ({"step_ids": ("ingest", "ingest")}, "duplicate"),
        ({"step_ids": ("not_a_registered_step",)}, "step_id"),
    ],
)
def test_prepare_refuses_every_invalid_reference_or_identifier(runtime_app, kwargs, match):
    projection = AgentRunProjection(runtime_app)
    call = {
        "run_ref": RUN_A,
        "agent": "agent:chase",
        "capability_id": "chase.request",
        "autonomy_level": "L2",
        "workflow_ref": chase_workflow_ref(CLAIM_A),
        "workflow_type": "DocumentChaseWorkflow",
        "step_ids": (),
    }
    call.update(kwargs)
    with Session(runtime_app.state.engine) as session, pytest.raises(
        (ValueError, ControlContractError), match=match
    ):
        projection.prepare(session, **call)


def test_prepare_lets_a_duplicate_workflow_id_violation_propagate(runtime_app):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, id=RUN_A, workflow_id=str(chase_workflow_ref(CLAIM_A)))
    with Session(runtime_app.state.engine) as session, pytest.raises(IntegrityError):
        projection.prepare(
            session,
            run_ref=RUN_B,
            agent="agent:chase",
            capability_id="chase.request",
            autonomy_level="L2",
            workflow_ref=chase_workflow_ref(CLAIM_A),
            workflow_type="DocumentChaseWorkflow",
        )


def _record_started(projection, run_ref=RUN_A, run_id=WORKFLOW_RUN_A, **overrides):
    call = {
        "run_ref": run_ref,
        "workflow_ref": str(agent_workflow_ref(run_ref)),
        "workflow_run_ref": run_id,
        "workflow_type": "PachaTestReviewWaitWorkflow",
        "worker_build_id": "a" * 40,
    }
    call.update(overrides)
    projection.record_started(**call)


def test_record_started_moves_pending_to_running_and_is_idempotent(runtime_app):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app)

    _record_started(projection)
    with Session(runtime_app.state.engine) as session:
        first = session.get(AgentRun, RUN_A)
        assert first.status == "running"
        assert first.workflow_run_id == WORKFLOW_RUN_A
        assert first.worker_build_id == "a" * 40
        assert first.last_synced_at is not None
        assert first.ended_at is None

    _record_started(projection)
    with Session(runtime_app.state.engine) as session:
        assert session.get(AgentRun, RUN_A).status == "running"


def test_record_started_accepts_a_continue_as_new_run_id_while_active(runtime_app):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, status="awaiting_review")

    _record_started(projection, run_id=WORKFLOW_RUN_A)
    _record_started(projection, run_id=WORKFLOW_RUN_B)

    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    # The new Run ID is taken; the lifecycle status is not disturbed.
    assert row.workflow_run_id == WORKFLOW_RUN_B
    assert row.status == "awaiting_review"


@pytest.mark.parametrize("terminal", ["blocked", "completed", "failed", "cancelled"])
def test_record_started_refuses_to_restart_a_terminal_row(runtime_app, terminal):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, status=terminal)
    with pytest.raises(AgentRunConflict, match="terminal"):
        _record_started(projection)


def test_record_started_refuses_a_workflow_id_or_type_mismatch(runtime_app):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app)
    with pytest.raises(AgentRunConflict, match="Workflow ID"):
        _record_started(projection, workflow_ref=str(agent_workflow_ref(RUN_B)))
    with pytest.raises(AgentRunConflict, match="Workflow type"):
        _record_started(projection, workflow_type="SomeOtherWorkflow")


def test_record_started_and_record_status_report_a_missing_row(runtime_app):
    projection = AgentRunProjection(runtime_app)
    with pytest.raises(AgentRunNotFound):
        _record_started(projection)
    with pytest.raises(AgentRunNotFound):
        projection.record_status(ControlResult(status="running", run_ref=RUN_A))


LEGAL_TRANSITIONS = [
    ("pending", "running"),
    ("pending", "failed"),
    ("pending", "cancelled"),
    ("running", "awaiting_review"),
    ("running", "blocked"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("awaiting_review", "running"),
    ("awaiting_review", "blocked"),
    ("awaiting_review", "completed"),
    ("awaiting_review", "failed"),
    ("awaiting_review", "cancelled"),
]

ILLEGAL_TRANSITIONS = [
    ("pending", "awaiting_review"),
    ("pending", "blocked"),
    ("pending", "completed"),
    ("running", "pending"),
    ("awaiting_review", "pending"),
    ("completed", "running"),
    ("completed", "failed"),
    ("failed", "running"),
    ("blocked", "completed"),
    ("cancelled", "running"),
]


@pytest.mark.parametrize(("start", "target"), LEGAL_TRANSITIONS)
def test_record_status_applies_every_legal_transition(runtime_app, start, target):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, status=start)
    projection.record_status(ControlResult(status=target, run_ref=RUN_A))
    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    assert row.status == target
    if target in {"blocked", "completed", "failed", "cancelled"}:
        assert row.ended_at is not None
    else:
        assert row.ended_at is None


@pytest.mark.parametrize(("start", "target"), ILLEGAL_TRANSITIONS)
def test_record_status_refuses_every_illegal_transition(runtime_app, start, target):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, status=start)
    with pytest.raises(AgentRunConflict):
        projection.record_status(ControlResult(status=target, run_ref=RUN_A))
    with Session(runtime_app.state.engine) as session:
        assert session.get(AgentRun, RUN_A).status == start


@pytest.mark.parametrize("status", AGENT_RUN_STATUSES)
def test_record_status_is_idempotent_for_the_same_status(runtime_app, status):
    projection = AgentRunProjection(runtime_app)
    _seed_run(runtime_app, status=status)
    projection.record_status(ControlResult(status=status, run_ref=RUN_A))
    with Session(runtime_app.state.engine) as session:
        first = session.get(AgentRun, RUN_A).ended_at
    projection.record_status(ControlResult(status=status, run_ref=RUN_A))
    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    assert row.status == status
    # A repeated terminal observation must not move the recorded end time.
    assert row.ended_at == first


def test_record_status_stores_the_supplied_event_reference_and_never_steps_or_error(runtime_app):
    projection = AgentRunProjection(runtime_app)
    _seed_run(
        runtime_app,
        status="running",
        steps=[{"step_id": "ingest", "status": "completed", "attempts": 1}],
        error={"code": "PARTIAL"},
    )

    projection.record_status(ControlResult(status="running", run_ref=RUN_A, event_ref=EVENT_A))
    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    assert row.last_workflow_event_ref == EVENT_A

    projection.record_status(
        ControlResult(
            status="awaiting_review",
            run_ref=RUN_A,
            event_ref=EVENT_A,
            review_event_ref=REVIEW_A,
        )
    )
    with Session(runtime_app.state.engine) as session:
        row = session.get(AgentRun, RUN_A)
    assert row.last_workflow_event_ref == REVIEW_A
    # Domain detail belongs to the T03-T06 Activities and is left untouched.
    assert row.steps == [{"step_id": "ingest", "status": "completed", "attempts": 1}]
    assert row.error == {"code": "PARTIAL"}


def test_record_status_requires_a_run_reference(runtime_app):
    projection = AgentRunProjection(runtime_app)
    with pytest.raises(ValueError, match="run_ref"):
        projection.record_status(ControlResult(status="running"))


# =============================================================================
# 5-8. dispatcher evolution
# =============================================================================


def _emit(app, event_type: str = "claim.created", payload: dict | None = None) -> str:
    with Session(app.state.engine) as session, session.begin():
        event = app.state.record_event(
            session,
            claim_id=None,
            event_type=event_type,
            payload=payload or {},
            actor="system",
            correlation_id=RUN_A,
        )
        return event.id


def _delivery_status(app, event_id: str, consumer: str) -> tuple[str, int]:
    with app.state.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, attempts FROM event_deliveries "
                "WHERE event_id = :event_id AND consumer = :consumer"
            ),
            {"event_id": event_id, "consumer": consumer},
        ).one_or_none()
    return (row[0], row[1]) if row is not None else ("", 0)


@pytest.fixture()
def bare_app(tmp_path):
    """An application whose only consumers are the ones a test registers."""

    app = create_app(_database_url(tmp_path, "dispatch"))
    dispatcher = app.state.dispatcher
    dispatcher._consumers.clear()  # noqa: SLF001 - deliberate test isolation
    return app


@pytest.mark.parametrize("limit", [0, -1, 501, 1.5, "50", True, False])
def test_a_supplied_limit_must_be_an_integer_batch_size(bare_app, limit):
    bare_app.state.dispatcher.register_consumer("probe", lambda event: None)
    with pytest.raises(ValueError, match="limit"):
        bare_app.state.dispatcher.dispatch_once(limit=limit)
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(bare_app.state.dispatcher.dispatch_once_async(limit=limit))


@pytest.mark.parametrize("limit", [1, 50, MAX_DISPATCH_LIMIT])
def test_the_boundary_limits_are_accepted(bare_app, limit):
    bare_app.state.dispatcher.register_consumer("probe", lambda event: None)
    assert bare_app.state.dispatcher.dispatch_once(limit=limit) == 0


def test_no_limit_preserves_the_existing_unbounded_behaviour(bare_app):
    seen: list[str] = []
    bare_app.state.dispatcher.register_consumer("probe", lambda event: seen.append(event.id))
    for _ in range(7):
        _emit(bare_app)
    assert bare_app.state.dispatcher.dispatch_once() == 7
    assert len(seen) == 7


def test_the_limit_counts_claimed_delivery_rows_not_source_events(bare_app):
    dispatcher = bare_app.state.dispatcher
    for name in ("alpha", "beta"):
        dispatcher.register_consumer(name, lambda event: None)
    for _ in range(3):
        _emit(bare_app)
    # Three events x two consumers = six delivery rows; the limit binds rows.
    assert dispatcher.dispatch_once(limit=4) == 4
    assert dispatcher.dispatch_once(limit=4) == 2


def test_a_limited_candidate_pass_retains_only_a_bounded_prefix_per_consumer(bare_app):
    dispatcher = bare_app.state.dispatcher
    for name in ("alpha", "beta"):
        dispatcher.register_consumer(name, lambda event: None)
    for _ in range(20):
        _emit(bare_app)

    pairs = dispatcher._eligible_pairs(["alpha", "beta"], limit=3)  # noqa: SLF001

    assert len(pairs) == 6
    assert [consumer for _event, consumer in pairs].count("alpha") == 3
    assert [consumer for _event, consumer in pairs].count("beta") == 3


def test_candidates_are_ordered_globally_by_event_sequence_then_consumer(bare_app):
    dispatcher = bare_app.state.dispatcher
    order: list[tuple[str, int]] = []
    for name in ("zulu", "alpha"):
        dispatcher.register_consumer(
            name, lambda event, name=name: order.append((name, event.seq))
        )
    first = _emit(bare_app)
    second = _emit(bare_app)
    dispatcher.dispatch_once()

    seqs = [seq for _name, seq in order]
    assert seqs == sorted(seqs), "ordering must be event-major, not consumer-major"
    with Session(bare_app.state.engine) as session:
        first_seq = session.get(Event, first).seq
        second_seq = session.get(Event, second).seq
    assert order == [
        ("alpha", first_seq),
        ("zulu", first_seq),
        ("alpha", second_seq),
        ("zulu", second_seq),
    ]


def test_synchronous_dispatch_refuses_an_async_consumer_without_leaking_a_coroutine(bare_app):
    async def consumer(event) -> None:  # noqa: RUF029 - the point is that it is async
        return None

    bare_app.state.dispatcher.register_consumer("async_probe", consumer)
    event_id = _emit(bare_app)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with pytest.raises(TypeError, match="dispatch_once_async"):
            bare_app.state.dispatcher.dispatch_once()
    # A detectable async consumer is rejected before a delivery is claimed:
    # the mistake must not spend retry budget or leave a pending row behind.
    assert _delivery_status(bare_app, event_id, "async_probe") == ("", 0)


def test_a_hidden_awaitable_is_persisted_as_failed_instead_of_pending(bare_app):
    async def result() -> None:
        return None

    def consumer(event):
        return result()

    bare_app.state.dispatcher.register_consumer("hidden_async", consumer)
    event_id = _emit(bare_app)

    with pytest.raises(TypeError, match="dispatch_once_async"):
        bare_app.state.dispatcher.dispatch_once()
    assert _delivery_status(bare_app, event_id, "hidden_async") == ("failed", 1)


def test_cancelling_while_waiting_for_the_dispatch_lock_cannot_orphan_it(bare_app):
    dispatcher = bare_app.state.dispatcher

    async def scenario() -> None:
        dispatcher._dispatch_lock.acquire()  # noqa: SLF001 - force lock contention
        task = asyncio.create_task(dispatcher.dispatch_once_async([]))
        try:
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            dispatcher._dispatch_lock.release()  # noqa: SLF001 - release test ownership
            await asyncio.sleep(0.05)
            assert not dispatcher._dispatch_lock.locked()  # noqa: SLF001
        finally:
            if dispatcher._dispatch_lock.locked():  # noqa: SLF001
                dispatcher._dispatch_lock.release()  # noqa: SLF001

    asyncio.run(scenario())


def test_cancellation_waits_for_an_inflight_sync_consumer_before_unlocking(bare_app):
    dispatcher = bare_app.state.dispatcher
    started = ThreadEvent()
    release = ThreadEvent()

    def consumer(event) -> None:
        started.set()
        release.wait(timeout=2)

    dispatcher.register_consumer("blocking_sync", consumer)
    event_id = _emit(bare_app)

    async def scenario() -> None:
        task = asyncio.create_task(dispatcher.dispatch_once_async())
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        try:
            await asyncio.sleep(0.02)
            assert not task.done(), "cancellation must not abandon the worker thread"
            assert dispatcher._dispatch_lock.locked()  # noqa: SLF001
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not dispatcher._dispatch_lock.locked()  # noqa: SLF001

    asyncio.run(scenario())
    assert _delivery_status(bare_app, event_id, "blocking_sync") == ("pending", 1)


def test_asynchronous_dispatch_awaits_an_async_consumer(bare_app):
    seen: list[str] = []

    async def consumer(event) -> None:
        await asyncio.sleep(0)
        seen.append(event.id)

    bare_app.state.dispatcher.register_consumer("async_probe", consumer)
    event_id = _emit(bare_app)

    assert asyncio.run(bare_app.state.dispatcher.dispatch_once_async()) == 1
    assert seen == [event_id]
    assert _delivery_status(bare_app, event_id, "async_probe") == ("succeeded", 1)


def test_asynchronous_dispatch_marks_success_only_after_the_await_returns(bare_app):
    observed: list[str] = []

    async def consumer(event) -> None:
        # The delivery must still be un-succeeded while the consumer is
        # mid-flight: for the Temporal bridge this is the window in which the
        # SDK has not yet acknowledged the start or Signal.
        observed.append(_delivery_status(bare_app, event.id, "async_probe")[0])
        await asyncio.sleep(0)

    bare_app.state.dispatcher.register_consumer("async_probe", consumer)
    event_id = _emit(bare_app)
    asyncio.run(bare_app.state.dispatcher.dispatch_once_async())

    assert observed == ["pending"]
    assert _delivery_status(bare_app, event_id, "async_probe")[0] == "succeeded"


def test_asynchronous_dispatch_also_drives_a_plain_synchronous_consumer(bare_app):
    seen: list[str] = []
    bare_app.state.dispatcher.register_consumer("sync_probe", lambda event: seen.append(event.id))
    event_id = _emit(bare_app)
    assert asyncio.run(bare_app.state.dispatcher.dispatch_once_async()) == 1
    assert seen == [event_id]
    assert _delivery_status(bare_app, event_id, "sync_probe")[0] == "succeeded"


def test_an_async_consumer_failure_retries_and_dead_letters_exactly_as_before(bare_app):
    from claim_core.outbox import MAX_ATTEMPTS

    async def consumer(event) -> None:
        raise RuntimeError("bridge unavailable")

    bare_app.state.dispatcher.register_consumer("async_probe", consumer)
    event_id = _emit(bare_app)

    asyncio.run(bare_app.state.dispatcher.dispatch_once_async())
    assert _delivery_status(bare_app, event_id, "async_probe") == ("failed", 1)

    # Retry timing is unchanged, so the clock has to move for later attempts.
    for attempt in range(2, MAX_ATTEMPTS + 1):
        bare_app.state.dispatcher._clock = lambda: datetime.now(UTC).replace(  # noqa: SLF001
            year=2099
        )
        asyncio.run(bare_app.state.dispatcher.dispatch_once_async())
        expected = "dead_letter" if attempt == MAX_ATTEMPTS else "failed"
        assert _delivery_status(bare_app, event_id, "async_probe") == (expected, attempt)

    with Session(bare_app.state.engine) as session:
        alerts = list(
            session.scalars(select(Event).where(Event.type == "ops.alert"))
        )
    assert len(alerts) == 1
    assert alerts[0].payload["failed_consumer"] == "async_probe"
    assert alerts[0].payload["subtype"] == "event_delivery_dead_letter"


def test_an_ops_alert_caused_by_a_consumer_stays_invisible_to_that_consumer(bare_app):
    """Regression guard: the async path must keep the existing suppression."""

    calls: list[str] = []

    async def consumer(event) -> None:
        calls.append(event.type)
        raise RuntimeError("always fails")

    bare_app.state.dispatcher.register_consumer("async_probe", consumer)
    _emit(bare_app)
    for _ in range(12):
        bare_app.state.dispatcher._clock = lambda: datetime.now(UTC).replace(  # noqa: SLF001
            year=2099
        )
        asyncio.run(bare_app.state.dispatcher.dispatch_once_async())
    assert "ops.alert" not in calls


def test_no_event_is_ever_deleted_by_a_bounded_pass(bare_app):
    bare_app.state.dispatcher.register_consumer("probe", lambda event: None)
    for _ in range(4):
        _emit(bare_app)
    bare_app.state.dispatcher.dispatch_once(limit=1)
    with Session(bare_app.state.engine) as session:
        assert session.scalar(select(Event.id).where(Event.id.is_not(None))) is not None
        assert len(list(session.scalars(select(Event)))) == 4


# =============================================================================
# 9-14. starter, mappings and Signal routing
# =============================================================================


class _FakeStarter:
    """Records exactly what a mapping asked the transport to do."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []

    async def start(self, *, workflow_type, workflow_ref, command) -> None:
        self.starts.append(
            {"workflow_type": workflow_type, "workflow_ref": workflow_ref, "command": command}
        )

    async def signal(self, *, workflow_ref, signal_name, signal) -> None:
        self.signals.append(
            {"workflow_ref": workflow_ref, "signal_name": signal_name, "signal": signal}
        )


def _event(event_type: str = "review.resolved", **overrides: Any) -> SimpleNamespace:
    values = {
        "id": EVENT_A,
        "type": event_type,
        "claim_id": CLAIM_A,
        "correlation_id": RUN_A,
        "payload": {},
        "seq": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _start_mapping(**overrides: Any) -> TemporalIntentMapping:
    values = {
        "event_type": "claim.created",
        "workflow_type": "SomeWorkflow",
        "workflow_id_builder": lambda event: agent_workflow_ref(event.correlation_id),
        "action": "start",
        "signal_name": None,
        "control_contract_type": ControlCommand,
    }
    values.update(overrides)
    return TemporalIntentMapping(**values)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"event_type": ""}, "event_type"),
        ({"event_type": "   "}, "event_type"),
        ({"workflow_type": ""}, "workflow_type"),
        ({"workflow_type": "*"}, "workflow_type"),
        ({"workflow_type": "Not Registered"}, "workflow_type"),
        ({"workflow_type": 7}, "workflow_type"),
        ({"workflow_type": int}, "workflow_type"),
        ({"workflow_id_builder": "not-callable"}, "workflow_id_builder"),
        ({"action": "cancel"}, "action"),
        ({"signal_name": "pacha_event"}, "signal_name"),
        ({"control_contract_type": ControlSignal}, "control_contract_type"),
    ],
)
def test_a_start_mapping_rejects_every_invalid_combination(overrides, match):
    with pytest.raises(ControlContractError, match=match):
        _start_mapping(**overrides)


def test_a_mapping_rejects_an_async_workflow_id_builder():
    async def build(event):
        return agent_workflow_ref(event.correlation_id)

    with pytest.raises(ControlContractError, match="synchronous"):
        _start_mapping(workflow_id_builder=build)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"signal_name": None}, "signal_name"),
        ({"signal_name": "invented_signal"}, "signal_name"),
        ({"control_contract_type": ControlCommand}, "control_contract_type"),
    ],
)
def test_a_signal_mapping_rejects_every_invalid_combination(overrides, match):
    values = {
        "event_type": "review.resolved",
        "workflow_type": "SomeWorkflow",
        "workflow_id_builder": lambda event: agent_workflow_ref(event.correlation_id),
        "action": "signal",
        "signal_name": "review_resolved",
        "control_contract_type": ControlSignal,
    }
    values.update(overrides)
    with pytest.raises(ControlContractError, match=match):
        TemporalIntentMapping(**values)


def test_a_duplicate_event_type_is_refused_at_consumer_construction():
    with pytest.raises(ControlContractError, match="more than once"):
        TemporalIntentConsumer(_FakeStarter(), [_start_mapping(), _start_mapping()])


def test_an_unknown_event_type_performs_no_temporal_call_and_returns_normally():
    starter = _FakeStarter()
    consumer = TemporalIntentConsumer(starter, [_start_mapping()])
    asyncio.run(consumer(_event("some.unmapped.event")))
    assert starter.starts == [] and starter.signals == []


def test_a_builder_returning_none_is_an_acknowledged_no_op():
    starter = _FakeStarter()
    consumer = TemporalIntentConsumer(
        starter, [_start_mapping(workflow_id_builder=lambda event: None)]
    )
    asyncio.run(consumer(_event("claim.created")))
    assert starter.starts == []


def test_a_builder_exception_propagates_so_the_delivery_retries():
    def explode(event):
        raise RuntimeError("resolver unavailable")

    consumer = TemporalIntentConsumer(
        _FakeStarter(), [_start_mapping(workflow_id_builder=explode)]
    )
    with pytest.raises(RuntimeError, match="resolver unavailable"):
        asyncio.run(consumer(_event("claim.created")))


def test_a_start_command_carries_only_the_four_specified_opaque_references():
    starter = _FakeStarter()
    consumer = TemporalIntentConsumer(starter, [_start_mapping()])
    asyncio.run(consumer(_event("claim.created")))

    command = starter.starts[0]["command"]
    assert isinstance(command, ControlCommand)
    assert command.as_control_mapping() == {
        "run_ref": RUN_A,
        "claim_ref": CLAIM_A,
        "trigger_event_ref": EVENT_A,
        "event_ref": EVENT_A,
    }


def test_a_start_is_refused_when_the_correlation_id_is_not_a_ulid():
    """The run ULID must already be committed; the consumer never mints one.

    The builder here resolves independently of the correlation id, so the
    refusal proves the consumer's own check rather than a builder that happened
    to reject the value first.
    """

    starter = _FakeStarter()
    consumer = TemporalIntentConsumer(
        starter, [_start_mapping(workflow_id_builder=lambda event: agent_workflow_ref(RUN_B))]
    )
    with pytest.raises(ControlContractError, match="run_ref"):
        asyncio.run(consumer(_event("claim.created", correlation_id="not-a-ulid")))
    assert starter.starts == []


def test_a_signal_carries_exactly_one_event_ulid():
    from support.temporal import review_wait_mapping

    starter = _FakeStarter()
    consumer = TemporalIntentConsumer(starter, [review_wait_mapping(TemporalIntentMapping)])
    asyncio.run(consumer(_event("review.resolved", payload={"agent_run_id": RUN_B})))

    delivered = starter.signals[0]
    assert delivered["signal_name"] == "review_resolved"
    assert delivered["workflow_ref"] == agent_workflow_ref(RUN_B)
    assert isinstance(delivered["signal"], ControlSignal)
    assert delivered["signal"].as_control_mapping() == {"event_ref": EVENT_A}


class _RecordingClient:
    """Captures the start/signal options `TemporalStarter` actually pins."""

    def __init__(self) -> None:
        self.start_kwargs: dict[str, Any] = {}
        self.signalled: list[tuple[str, str, Any]] = []
        self.handles: list[str] = []

    async def start_workflow(self, workflow_type, arg, **kwargs):
        self.start_kwargs = {"workflow_type": workflow_type, "arg": arg, **kwargs}
        return SimpleNamespace(id=kwargs["id"])

    def get_workflow_handle(self, workflow_id: str):
        self.handles.append(workflow_id)
        client = self

        class _Handle:
            async def signal(self, name, payload):
                client.signalled.append((workflow_id, name, payload))

        return _Handle()


def test_the_starter_fixes_the_control_queue_and_both_duplicate_policies():
    client = _RecordingClient()
    starter = TemporalStarter(client, local_config())
    asyncio.run(
        starter.start(
            workflow_type="SomeWorkflow",
            workflow_ref=agent_workflow_ref(RUN_A),
            command=ControlCommand(run_ref=RUN_A),
        )
    )

    assert client.start_kwargs["task_queue"] == "pacha-test-control-v1"
    assert client.start_kwargs["id"] == str(agent_workflow_ref(RUN_A))
    assert client.start_kwargs["id_reuse_policy"] is REQUIRED_ID_REUSE_POLICY
    assert client.start_kwargs["id_conflict_policy"] is REQUIRED_ID_CONFLICT_POLICY
    # No forbidden SDK surface is offered at all.
    for forbidden in ("memo", "search_attributes", "static_summary", "static_details",
                      "headers", "cron_schedule"):
        assert forbidden not in client.start_kwargs


@pytest.mark.parametrize("signal_name", sorted(STANDARD_SIGNAL_NAMES))
def test_every_standard_signal_name_is_accepted(signal_name):
    client = _RecordingClient()
    starter = TemporalStarter(client, local_config())
    asyncio.run(
        starter.signal(
            workflow_ref=agent_workflow_ref(RUN_A),
            signal_name=signal_name,
            signal=ControlSignal(event_ref=EVENT_A),
        )
    )
    assert client.signalled[0][1] == signal_name
    assert client.handles == [str(agent_workflow_ref(RUN_A))]


@pytest.mark.parametrize(
    "signal_name", ["", "pacha_events", "review.resolved", "PACHA_EVENT", "anything"]
)
def test_any_other_signal_name_is_refused(signal_name):
    starter = TemporalStarter(_RecordingClient(), local_config())
    with pytest.raises(ControlContractError, match="signal_name"):
        asyncio.run(
            starter.signal(
                workflow_ref=agent_workflow_ref(RUN_A),
                signal_name=signal_name,
                signal=ControlSignal(event_ref=EVENT_A),
            )
        )


def test_the_standard_signal_registry_is_exactly_the_six_declared_names():
    assert STANDARD_SIGNAL_NAMES == {
        "pacha_event",
        "review_resolved",
        "claim_terminal",
        "document_received",
        "snooze_changed",
        "inbound_received",
    }


def test_the_starter_exposes_no_query_update_terminate_or_cancel_method():
    surface = {name for name in vars(TemporalStarter) if not name.startswith("_")}
    assert surface == {"start", "signal"}


def test_the_production_intent_mapping_registry_includes_t03_and_t04():
    """T04 adds document start and recovery intents to T03's chase surface."""

    assert {
        (mapping.event_type, mapping.action, mapping.signal_name)
        for mapping in TEMPORAL_INTENT_MAPPINGS
    } == {
        ("document.received", "start", None),
        ("document.stage_recovered", "signal", "pacha_event"),
        ("document.split_resolved", "signal", "review_resolved"),
        ("chase.workflow_requested", "start", None),
        ("chase.item_requested", "signal", "pacha_event"),
        ("chase.item_received", "signal", "document_received"),
        ("chase.item_verified", "signal", "document_received"),
        ("chase.item_rejected", "signal", "pacha_event"),
        ("chase.item_waived", "signal", "pacha_event"),
        ("chase.item_snoozed", "signal", "snooze_changed"),
        ("chase.reminder_sent", "signal", "pacha_event"),
        ("chase.complete", "signal", "pacha_event"),
        ("chase.cancelled", "signal", "claim_terminal"),
        ("chase.inbound_received", "signal", "inbound_received"),
        ("chase.review_resolved", "signal", "review_resolved"),
    }


# =============================================================================
# 15-17. system Activities
# =============================================================================


class _FakeDispatcher:
    def __init__(self, attempted: int = 0) -> None:
        self.attempted = attempted
        self.async_calls: list[tuple[list[str], int | None]] = []
        self.sync_calls: list[tuple[list[str], int | None]] = []
        self.consumer_names = frozenset({"ledger", "sla", "external_refs", "temporal_intent"})

    async def dispatch_once_async(self, consumers=None, *, limit=None) -> int:
        self.async_calls.append((list(consumers or []), limit))
        return self.attempted

    def dispatch_once(self, consumers=None, *, limit=None) -> int:
        self.sync_calls.append((list(consumers or []), limit))
        return self.attempted


class _FakeSla:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, now=None) -> int:
        self.calls += 1
        return 0


class _FakeLedger:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.verifications = 0
        self.consumed: list[Any] = []

    def run_nightly_verification(self) -> dict[str, Any]:
        self.verifications += 1
        return {"ok": self.ok, "checked": 3, "first_bad_seq": None if self.ok else 2}

    def consume(self, event) -> None:  # pragma: no cover - must never be called
        self.consumed.append(event)


def _fake_app(*, attempted: int = 0, ledger_ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            dispatcher=_FakeDispatcher(attempted),
            sla_engine=_FakeSla(),
            ledger=_FakeLedger(ledger_ok),
            clock=lambda: datetime.now(UTC),
        )
    )


def test_system_activities_refuse_construction_without_the_temporal_bridge():
    app = _fake_app()
    app.state.dispatcher.consumer_names = frozenset({"ledger", "sla"})
    with pytest.raises(RuntimeError, match="temporal_intent"):
        SystemActivities(app)


@pytest.mark.parametrize("missing", ["dispatcher", "sla_engine", "ledger"])
def test_system_activities_refuse_construction_without_a_required_service(missing):
    app = _fake_app()
    delattr(app.state, missing)
    with pytest.raises(RuntimeError, match=missing):
        SystemActivities(app)


def test_dispatch_nonledger_events_drives_every_consumer_except_ledger():
    app = _fake_app()
    activities = SystemActivities(app)
    asyncio.run(activities.dispatch_nonledger_events())

    consumers, limit = app.state.dispatcher.async_calls[0]
    assert consumers == ["external_refs", "sla", "temporal_intent"]
    assert limit == DISPATCH_BATCH_SIZE
    # It touches nothing else.
    assert app.state.dispatcher.sync_calls == []
    assert app.state.sla_engine.calls == 0
    assert app.state.ledger.verifications == 0


def test_append_ledger_batch_drives_only_the_ledger_consumer():
    app = _fake_app()
    activities = SystemActivities(app)
    asyncio.run(activities.append_ledger_batch())

    consumers, limit = app.state.dispatcher.sync_calls[0]
    assert consumers == ["ledger"]
    assert limit == DISPATCH_BATCH_SIZE
    assert app.state.dispatcher.async_calls == []
    # The Activity never inserts an audit row itself.
    assert app.state.ledger.consumed == []


def test_evaluate_slas_calls_the_engine_once_and_passes_no_clock():
    app = _fake_app()
    activities = SystemActivities(app)
    result = asyncio.run(activities.evaluate_slas())

    assert result.status == "completed"
    assert app.state.sla_engine.calls == 1
    assert app.state.dispatcher.async_calls == [] and app.state.dispatcher.sync_calls == []


def test_verify_ledger_calls_the_nightly_verification_once():
    app = _fake_app()
    activities = SystemActivities(app)
    assert asyncio.run(activities.verify_ledger()).status == "completed"
    assert app.state.ledger.verifications == 1


@pytest.mark.parametrize(
    ("attempted", "status"),
    [(0, "completed"), (1, "completed"), (49, "completed"), (50, "running")],
)
def test_a_batch_returns_running_only_when_it_filled(attempted, status):
    app = _fake_app(attempted=attempted)
    activities = SystemActivities(app)
    assert asyncio.run(activities.dispatch_nonledger_events()).status == status
    assert asyncio.run(activities.append_ledger_batch()).status == status


def test_an_unhealthy_verification_becomes_a_sanitised_payload_diverged():
    from temporalio.exceptions import ApplicationError

    app = _fake_app(ledger_ok=False)
    activities = SystemActivities(app)
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(activities.verify_ledger())

    assert caught.value.type == "payload_diverged"
    assert caught.value.message == "payload_diverged"
    assert caught.value.non_retryable is True
    # Neither the failing sequence number nor any row detail escapes.
    assert "2" not in str(caught.value)


def test_an_infrastructure_error_becomes_a_sanitised_activity_internal():
    from temporalio.exceptions import ApplicationError

    app = _fake_app()

    async def explode(consumers=None, *, limit=None):
        raise RuntimeError("connection to claims_db refused for user pacha_app")

    app.state.dispatcher.dispatch_once_async = explode
    activities = SystemActivities(app)
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(activities.dispatch_nonledger_events())

    assert caught.value.type == "activity_internal"
    assert "claims_db" not in str(caught.value)
    assert "pacha_app" not in str(caught.value)


def test_the_registration_helpers_split_control_and_ledger_exactly():
    app = _fake_app()
    system = SystemActivities(app)
    agent_runs = AgentRunActivities(
        AgentRunProjection(SimpleNamespace(state=SimpleNamespace(engine=create_engine("sqlite://")))),
        worker_build_id="a" * 40,
    )

    control = {fn.__name__ for fn in control_activity_registrations(system, agent_runs)}
    ledger = {fn.__name__ for fn in ledger_activity_registrations(system)}
    assert control == {
        "dispatch_nonledger_events",
        "evaluate_slas",
        "record_agent_run_started",
        "record_agent_run_status",
    }
    assert ledger == {"append_ledger_batch", "verify_ledger"}
    assert control.isdisjoint(ledger)


# =============================================================================
# 18. Workflow module import hygiene
# =============================================================================

WORKFLOW_SOURCE = REPO / "platform" / "orchestration" / "workflows.py"

PERMITTED_WORKFLOW_IMPORTS = {
    "temporalio",
    "temporalio.workflow",
    "temporalio.common",
    "orchestration.contracts",
    "orchestration.errors",
    "orchestration.ids",
    "orchestration.policies",
    "__future__",
}


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_workflow_module_imports_only_deterministic_pass_through_modules():
    assert _imported_modules(WORKFLOW_SOURCE) <= PERMITTED_WORKFLOW_IMPORTS


@pytest.mark.parametrize(
    "forbidden",
    [
        "sqlalchemy",
        "claim_core",
        "agent_runtime",
        "orchestration.activities",
        "orchestration.config",
        "orchestration.client",
        "orchestration.codec",
        "orchestration.worker",
        "orchestration.starter",
        "os",
    ],
)
def test_the_workflow_module_imports_no_database_app_config_codec_or_activity_module(forbidden):
    assert forbidden not in _imported_modules(WORKFLOW_SOURCE)
    # Not even as a package-root import that would resolve lazily.
    assert f"import {forbidden}" not in WORKFLOW_SOURCE.read_text(encoding="utf-8")


def _executable_source(path: pathlib.Path) -> str:
    """The module's code with docstrings and comments removed.

    Scanning raw text would match the prose that *documents* the prohibition,
    so the check would pass or fail for the wrong reason.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_workflow_module_adds_no_timer_randomness_or_continue_as_new():
    source = _executable_source(WORKFLOW_SOURCE)
    for forbidden in (
        "continue_as_new",
        "asyncio.sleep",
        "workflow.sleep",
        "workflow.now",
        "workflow.random",
        "workflow.start_timer",
        "workflow.patched",
        "random",
        "logging",
    ):
        assert forbidden not in source


def test_every_system_workflow_is_registered_with_its_exact_name_and_pinned():
    from temporalio.common import VersioningBehavior

    names = []
    for workflow_class in SYSTEM_WORKFLOWS:
        definition = workflow_class.__temporal_workflow_definition
        names.append(definition.name)
        assert definition.versioning_behavior is VersioningBehavior.PINNED
        # No system Workflow declares a Signal or a Query handler.
        assert not definition.signals
        assert not definition.queries
    assert names == [
        "OutboxDrainWorkflow",
        "LedgerDrainWorkflow",
        "SlaEvaluationWorkflow",
        "LedgerVerificationWorkflow",
    ]


def test_the_drain_cap_bounds_one_execution_at_five_hundred_attempts():
    assert MAX_DRAIN_BATCHES * DISPATCH_BATCH_SIZE == MAX_DISPATCH_LIMIT == 500


# =============================================================================
# application wiring (§11) — Temporal stays out of the request path
# =============================================================================


def test_create_app_never_connects_to_temporal(tmp_path):
    """§11 — the claim APIs must be fully usable with Temporal stopped.

    Run in a subprocess with a clean interpreter, because the assertion is
    about what `create_app` *imports*: in this process the Temporal modules are
    already loaded by the suite itself.
    """

    program = (
        "import sys\n"
        "from claim_core.app import create_app\n"
        f"create_app({str(_database_url(tmp_path, 'no_temporal'))!r})\n"
        "heavy = {'client', 'codec', 'config', 'worker', 'starter', 'activities', 'workflows'}\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name.startswith('orchestration.') and name.split('.', 1)[1] in heavy\n"
        ")\n"
        "print(','.join(leaked))\n"
    )
    assert _subprocess_stdout(program) == ""

    source = (REPO / "platform" / "claim_core" / "app.py").read_text(encoding="utf-8")
    assert "orchestration" not in source
    assert "temporalio" not in source
    # And the bridge consumer is wired by the Worker call site, never by the app.
    app = create_app(_database_url(tmp_path, "no_temporal_consumers"))
    assert "temporal_intent" not in app.state.dispatcher.consumer_names


def test_the_claim_apis_work_with_no_temporal_server(tmp_path):
    """The acceptance gate, exercised rather than asserted about."""

    from fastapi.testclient import TestClient

    client = TestClient(create_app(_database_url(tmp_path, "api_no_temporal")))
    headers = {"X-Actor": "agent:intake"}

    created = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=headers
    )
    assert created.status_code == 201
    claim_id = created.json()["id"]

    assert client.get(f"/claims/{claim_id}", headers=headers).status_code == 200
    assert client.get("/claims", headers=headers).status_code == 200
    assert client.get(f"/claims/{claim_id}/timeline", headers=headers).status_code == 200
    assert client.get("/events", headers=headers).status_code == 200


# =============================================================================
# 19. ledger single writer
# =============================================================================


def test_the_advisory_lock_uses_the_exact_owner_approved_key():
    assert LEDGER_ADVISORY_LOCK_KEY == "pacha:audit-ledger-writer"
    assert (
        LEDGER_ADVISORY_LOCK_SQL
        == "SELECT pg_advisory_xact_lock(hashtext('pacha:audit-ledger-writer'))"
    )
    source = (REPO / "platform" / "claim_core" / "ledger.py").read_text(encoding="utf-8")
    assert "hashtext('audit_ledger_writer')" not in source, "the superseded key must be gone"


def test_no_second_audit_ledger_insert_path_exists():
    """Only `LedgerWriter._append` may add a row (PRD-00 single-writer).

    `claim_core.models` is excluded by name because it *declares* the mapped
    class rather than instantiating one; every other construction site is a
    second writer.
    """

    declaring = REPO / "platform" / "claim_core" / "models.py"
    writers: list[str] = []
    for root in ("platform", "agents", "console"):
        for path in (REPO / root).rglob("*.py"):
            if "alembic" in path.parts or path == declaring:
                continue
            source = path.read_text(encoding="utf-8")
            if "AuditLedgerRow(" in source or "INSERT INTO audit_ledger" in source:
                writers.append(str(path.relative_to(REPO)))
    assert writers == ["platform/claim_core/ledger.py"]

    # And the only Activity that appends goes through the dispatcher, never
    # `LedgerWriter.consume` directly.
    activities = (REPO / "platform" / "orchestration" / "activities.py").read_text(
        encoding="utf-8"
    )
    assert "AuditLedgerRow" not in activities
    assert ".consume(" not in activities


# =============================================================================
# 20. package-root laziness
# =============================================================================


def test_importing_the_deterministic_modules_still_pulls_in_nothing_heavy():
    program = (
        "import sys\n"
        "import orchestration.contracts, orchestration.errors\n"
        "import orchestration.ids, orchestration.policies\n"
        "leaked = [name for name in ("
        "'orchestration.client', 'orchestration.codec', 'orchestration.config',"
        "'orchestration.worker', 'orchestration.starter', 'orchestration.activities',"
        "'orchestration.workflows') if name in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    assert _subprocess_stdout(program) == ""


def test_the_package_root_exports_temporal_starter_lazily():
    import orchestration

    assert "TemporalStarter" in orchestration.__all__
    assert orchestration.TemporalStarter is TemporalStarter


# =============================================================================
# migration contract (§4.2) — SQLite here, PostgreSQL in the required tier
# =============================================================================

LEGACY_ROWS = (
    ("01JZ8QG1B2C3D4E5F6G7H8J9K0", "running"),
    ("01JZ8QH1B2C3D4E5F6G7H8J9K0", "completed"),
)


def _insert_pre_migration_rows(engine, rows=LEGACY_ROWS) -> None:
    with engine.begin() as connection:
        for run_id, status in rows:
            connection.execute(
                text(
                    "INSERT INTO agent_runs "
                    "(id, agent, capability_id, status, steps, autonomy_level, started_at) "
                    "VALUES (:id, 'agent:intake', 'intake.triage', :status, '[]', 'L1', :now)"
                ),
                {"id": run_id, "status": status, "now": datetime.now(UTC)},
            )


def _insert_post_migration_row(engine, run_id: str, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, agent, capability_id, workflow_id, workflow_type, status, steps, "
                "autonomy_level, started_at) VALUES "
                "(:id, 'agent:intake', 'intake.triage', :workflow_id, 'DocumentChaseWorkflow', "
                ":status, '[]', 'L1', :now)"
            ),
            {
                "id": run_id,
                "workflow_id": str(agent_workflow_ref(run_id)),
                "status": status,
                "now": datetime.now(UTC),
            },
        )


def _index_names(engine, table: str) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes(table)}


@pytest.mark.schema_isolated
def test_migration_0016_adds_the_exact_columns_constraints_and_indexes(tmp_path):
    url = _database_url(tmp_path, "m0016_shape")
    config = _alembic_config(url)
    command.upgrade(config, "0015_projections")
    engine = create_engine(url)
    _insert_pre_migration_rows(engine)
    command.upgrade(config, "0016_temporal_runtime")

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("agent_runs")}
    assert set(columns) == set(EXPECTED_COLUMNS)
    assert columns["workflow_id"]["nullable"] is False
    assert columns["workflow_type"]["nullable"] is False
    for optional in ("workflow_run_id", "worker_build_id", "last_workflow_event_ref",
                     "last_synced_at"):
        assert columns[optional]["nullable"] is True

    unique = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("agent_runs")
    }
    assert unique.get("uq_agent_runs_workflow_id") == ["workflow_id"]
    assert _index_names(engine, "agent_runs") >= {"ix_agent_runs_status", "ix_agent_runs_claim"}

    checks = " ".join(
        str(check["sqltext"]) for check in inspector.get_check_constraints("agent_runs")
    )
    for status in AGENT_RUN_STATUSES:
        assert f"'{status}'" in checks


@pytest.mark.schema_isolated
def test_migration_0016_backfills_every_pre_existing_row_exactly(tmp_path):
    url = _database_url(tmp_path, "m0016_backfill")
    config = _alembic_config(url)
    command.upgrade(config, "0015_projections")
    engine = create_engine(url)
    _insert_pre_migration_rows(engine)
    command.upgrade(config, "0016_temporal_runtime")

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, workflow_id, workflow_type, worker_build_id, workflow_run_id, "
                "last_workflow_event_ref, last_synced_at, status FROM agent_runs ORDER BY id"
            )
        ).all()

    assert len(rows) == len(LEGACY_ROWS)
    for row, (run_id, status) in zip(rows, LEGACY_ROWS, strict=True):
        assert row[0] == run_id
        assert row[1] == f"pacha.legacy.agent.{run_id}"
        assert row[2] == "LegacyAgentRun"
        assert row[3] == "legacy-celery"
        assert row[4] is None and row[5] is None and row[6] is None
        assert row[7] == status  # existing statuses remain valid


@pytest.mark.schema_isolated
def test_migration_0016_enforces_workflow_id_uniqueness(tmp_path):
    from sqlalchemy.exc import IntegrityError as SaIntegrityError

    url = _database_url(tmp_path, "m0016_unique")
    config = _alembic_config(url)
    command.upgrade(config, "0016_temporal_runtime")
    engine = create_engine(url)
    _insert_post_migration_row(engine, "01JZ8QJ1B2C3D4E5F6G7H8J9K0", "running")

    with pytest.raises(SaIntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, agent, capability_id, workflow_id, workflow_type, status, steps, "
                "autonomy_level, started_at) VALUES "
                "('01JZ8QK1B2C3D4E5F6G7H8J9K0', 'agent:intake', 'intake.triage', "
                ":workflow_id, 'DocumentChaseWorkflow', 'running', '[]', 'L1', :now)"
            ),
            {
                "workflow_id": str(agent_workflow_ref("01JZ8QJ1B2C3D4E5F6G7H8J9K0")),
                "now": datetime.now(UTC),
            },
        )


@pytest.mark.schema_isolated
def test_migration_0016_accepts_all_seven_statuses_and_refuses_an_unknown_one(tmp_path):
    from sqlalchemy.exc import IntegrityError as SaIntegrityError

    url = _database_url(tmp_path, "m0016_status")
    config = _alembic_config(url)
    command.upgrade(config, "0016_temporal_runtime")
    engine = create_engine(url)

    for index, status in enumerate(AGENT_RUN_STATUSES):
        _insert_post_migration_row(engine, f"01JZ8QM1B2C3D4E5F6G7H8J9{index:02d}", status)

    with pytest.raises(SaIntegrityError):
        _insert_post_migration_row(engine, "01JZ8QN1B2C3D4E5F6G7H8J9K0", "quarantined")


@pytest.mark.schema_isolated
def test_migration_0016_downgrades_cleanly_and_preserves_legacy_rows(tmp_path):
    url = _database_url(tmp_path, "m0016_down")
    config = _alembic_config(url)
    command.upgrade(config, "0015_projections")
    engine = create_engine(url)
    _insert_pre_migration_rows(engine)
    command.upgrade(config, "0016_temporal_runtime")
    command.downgrade(config, "0015_projections")

    columns = {column["name"] for column in inspect(engine).get_columns("agent_runs")}
    assert columns == {
        "id", "agent", "capability_id", "claim_id", "trigger_event", "status",
        "steps", "autonomy_level", "error", "started_at", "ended_at",
    }
    assert _index_names(engine, "agent_runs").isdisjoint(
        {"ix_agent_runs_status", "ix_agent_runs_claim"}
    )
    with engine.connect() as connection:
        surviving = connection.execute(
            text("SELECT id, status FROM agent_runs ORDER BY id")
        ).all()
    assert [tuple(row) for row in surviving] == list(LEGACY_ROWS)


@pytest.mark.schema_isolated
def test_migration_0016_downgrades_an_empty_database_cleanly(tmp_path):
    url = _database_url(tmp_path, "m0016_empty")
    config = _alembic_config(url)
    command.upgrade(config, "0016_temporal_runtime")
    command.downgrade(config, "0015_projections")
    columns = {column["name"] for column in inspect(create_engine(url)).get_columns("agent_runs")}
    assert "workflow_id" not in columns


@pytest.mark.schema_isolated
@pytest.mark.parametrize("status", ["pending", "cancelled"])
def test_migration_0016_refuses_to_downgrade_rather_than_corrupt_a_new_status(tmp_path, status):
    url = _database_url(tmp_path, f"m0016_refuse_{status}")
    config = _alembic_config(url)
    command.upgrade(config, "0016_temporal_runtime")
    engine = create_engine(url)
    _insert_post_migration_row(engine, "01JZ8QP1B2C3D4E5F6G7H8J9K0", status)

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, "0015_projections")

    # Left wholly unapplied: the row and the widened schema both survive.
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM agent_runs WHERE id = '01JZ8QP1B2C3D4E5F6G7H8J9K0'")
        ).scalar_one() == status
    assert "workflow_id" in {
        column["name"] for column in inspect(engine).get_columns("agent_runs")
    }


@pytest.mark.schema_isolated
def test_migration_0016_upgrades_again_after_a_clean_downgrade(tmp_path):
    url = _database_url(tmp_path, "m0016_replay")
    config = _alembic_config(url)
    command.upgrade(config, "0015_projections")
    engine = create_engine(url)
    _insert_pre_migration_rows(engine)

    command.upgrade(config, "0016_temporal_runtime")
    command.downgrade(config, "0015_projections")
    command.upgrade(config, "0016_temporal_runtime")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT workflow_id, workflow_type, worker_build_id FROM agent_runs ORDER BY id")
        ).all()
    assert [row[0] for row in rows] == [f"pacha.legacy.agent.{run}" for run, _ in LEGACY_ROWS]
    assert {row[1] for row in rows} == {"LegacyAgentRun"}
    assert {row[2] for row in rows} == {"legacy-celery"}
    assert _index_names(engine, "agent_runs") >= {"ix_agent_runs_status", "ix_agent_runs_claim"}
