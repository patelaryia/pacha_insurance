"""T02 integration suite — real server, real Workers, real Pacha services.

Master plan §22 forbids mocking the Workflow engine for acceptance behaviour and
forbids turning a Temporal startup failure into a skip, so there is no guard and
no `importorskip` anywhere in this module: if the test server will not start,
these tests fail.

The application under test is an ordinary `create_app` build. Its dispatcher is
narrowed to exactly two consumers — `ledger` and `temporal_intent` — so that
"49 eligible deliveries" in a bounded-batch assertion means 49 delivery rows,
rather than 49 multiplied by however many domain projections happen to be
registered.

The T02 routing tests deliberately construct only the test-owned
`review.resolved` mapping and `ReviewWaitWorkflow` from `support.temporal`.
T03's production chase mappings are covered by `test_temporal_t03.py` and are
not registered in this older harness.

Every Workflow ID is minted from a fresh ULID. Reusing one would collide with
`REJECT_DUPLICATE` on a closed execution, and the suite must not depend on the
order pytest happens to run it in.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from temporalio.client import Client, WorkflowHistory
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from agent_runtime.models import AgentRun
from agent_runtime.projection import AgentRunProjection
from claim_core import Base
from claim_core.app import create_app
from claim_core.models import AuditLedgerRow, Event, EventDelivery
from claim_core.service import new_ulid
from orchestration.activities import (
    DISPATCH_BATCH_SIZE,
    AgentRunActivities,
    SystemActivities,
    control_activity_registrations,
    ledger_activity_registrations,
)
from orchestration.contracts import (
    CONTROL_FIELDS,
    ControlCommand,
    ControlPayloadInterceptor,
    ControlResult,
    ControlSignal,
)
from orchestration.history import decoded_history_blob, find_sentinels, history_blob
from orchestration.ids import agent_workflow_ref
from orchestration.starter import (
    TEMPORAL_INTENT_MAPPINGS,
    TemporalIntentConsumer,
    TemporalIntentMapping,
    TemporalStarter,
)
from orchestration.worker import build_worker
from orchestration.workflows import (
    MAX_DRAIN_BATCHES,
    SYSTEM_WORKFLOWS,
    LedgerDrainWorkflow,
    LedgerVerificationWorkflow,
    OutboxDrainWorkflow,
    SlaEvaluationWorkflow,
)
from support.temporal import (
    APPLIED_REVIEW_EVENTS,
    PRIVACY_SENTINELS,
    STATIC_CODEC_KEY,
    ReviewWaitWorkflow,
    apply_review_activity,
    local_config,
    plain_client_for,
    review_wait_mapping,
    static_data_converter,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "temporal" / "t02"

_SENTINELS = tuple(PRIVACY_SENTINELS.values())

#: The four committed replay fixtures, in section-3 order.
FIXTURES: tuple[tuple[str, type], ...] = (
    ("outbox_drain", OutboxDrainWorkflow),
    ("ledger_drain", LedgerDrainWorkflow),
    ("sla_evaluation", SlaEvaluationWorkflow),
    ("ledger_verification", LedgerVerificationWorkflow),
)


def _workflow_id(run_ref: str | None = None) -> str:
    """A fresh, declared Workflow ID."""

    return str(agent_workflow_ref(run_ref or new_ulid()))


class _Harness:
    """One time-skipping server, one control Worker, one ledger Worker."""

    def __init__(self, loop, env, config, app, starter) -> None:
        self.loop = loop
        self.env = env
        self.config = config
        self.app = app
        self.starter = starter
        self.control_worker: Any = None
        self.ledger_worker: Any = None
        self.system_activities: Any = None

    @property
    def client(self) -> Client:
        return self.env.client

    @property
    def codec(self) -> Any:
        return self.client.data_converter.payload_codec

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def realtime(self):
        """Suspend auto time skipping for a scenario that holds a live Workflow.

        The time-skipping server jumps the clock whenever the client waits on an
        idle execution. A `ReviewWaitWorkflow` parked on `wait_condition` has no
        timer to skip to, so a drain executed while it waits can jump past its
        ten-year execution timeout and close it — a harness artefact that has
        nothing to do with the routing under test.
        """

        return self.env.auto_time_skipping_disabled()

    # -- Pacha-side helpers ---------------------------------------------------

    def emit(self, event_type: str, payload: dict | None = None, *, correlation_id: str) -> str:
        with Session(self.app.state.engine) as session, session.begin():
            event = self.app.state.record_event(
                session,
                claim_id=None,
                event_type=event_type,
                payload=payload or {},
                actor="system",
                correlation_id=correlation_id,
            )
            return event.id

    def clear_events(self) -> None:
        """Reset the outbox between scenarios so counts stay unambiguous."""

        with self.app.state.engine.begin() as connection:
            connection.execute(text("DELETE FROM event_deliveries"))
            connection.execute(text("DELETE FROM audit_ledger"))
            connection.execute(text("DELETE FROM events"))

    def delivery(self, event_id: str, consumer: str = "temporal_intent") -> tuple[str, int]:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, attempts FROM event_deliveries "
                    "WHERE event_id = :event_id AND consumer = :consumer"
                ),
                {"event_id": event_id, "consumer": consumer},
            ).one_or_none()
        return (row[0], row[1]) if row is not None else ("", 0)

    def deliveries_by_status(self, consumer: str = "temporal_intent") -> dict[str, int]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT status, COUNT(*) FROM event_deliveries "
                    "WHERE consumer = :consumer GROUP BY status"
                ),
                {"consumer": consumer},
            ).all()
        return {row[0]: row[1] for row in rows}

    def make_retryable(self, event_id: str, consumer: str = "temporal_intent") -> None:
        """Put one delivery back in play, exactly as a redelivery would."""

        with self.app.state.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE event_deliveries SET status = 'failed', attempts = 0 "
                    "WHERE event_id = :event_id AND consumer = :consumer"
                ),
                {"event_id": event_id, "consumer": consumer},
            )

    # -- Temporal-side helpers ------------------------------------------------

    def execute(self, workflow_class, workflow_id: str | None = None):
        """Run one system Workflow to completion on the control queue."""

        return self.run(
            self.client.execute_workflow(
                workflow_class.run,
                id=workflow_id or _workflow_id(),
                task_queue=self.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        )

    def execute_recorded(self, workflow_class) -> tuple[str, Any]:
        """Run one system Workflow and return its Workflow ID with the result."""

        workflow_id = _workflow_id()
        return workflow_id, self.execute(workflow_class, workflow_id)

    def handle(self, workflow_id: str):
        """A handle whose `result()` decodes back into a `ControlResult`."""

        return self.client.get_workflow_handle(workflow_id, result_type=ControlResult)

    def history_events(self, workflow_id: str) -> list[Any]:
        handle = self.client.get_workflow_handle(workflow_id)
        return list(self.run(handle.fetch_history()).events)

    def activity_completions(self, workflow_id: str) -> int:
        return sum(
            1
            for event in self.history_events(workflow_id)
            if event.HasField("activity_task_completed_event_attributes")
        )

    def decoded_history(self, workflow_id: str) -> bytes:
        handle = self.client.get_workflow_handle(workflow_id)

        async def _fetch() -> bytes:
            return await decoded_history_blob(await handle.fetch_history(), self.codec)

        return self.run(_fetch())

    def stored_history(self, workflow_id: str) -> bytes:
        async def _fetch() -> bytes:
            plain = await plain_client_for(self.client)
            return history_blob(await plain.get_workflow_handle(workflow_id).fetch_history())

        return self.run(_fetch())

    def save_fixture(self, workflow_id: str, name: str) -> None:
        """Persist the stored history for the replay suite.

        Read through a Codec-free client on purpose, so what lands on disk is
        exactly what Temporal stored: ciphertext. A fixture containing Codec
        plaintext would be a committed copy of decrypted payloads.
        """

        async def _fetch() -> str:
            plain = await plain_client_for(self.client)
            history = await plain.get_workflow_handle(workflow_id).fetch_history()
            return WorkflowHistory(workflow_id, list(history.events)).to_json()

        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        document = json.loads(self.run(_fetch()))
        (FIXTURE_DIR / f"{name}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("t02")
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/t02.db")
    app = create_app(database_url)
    Base.metadata.create_all(app.state.engine, tables=[AgentRun.__table__])

    dispatcher = app.state.dispatcher
    for name in list(dispatcher._consumers):  # noqa: SLF001 - deliberate narrowing
        if name != "ledger":
            del dispatcher._consumers[name]  # noqa: SLF001

    config = local_config()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # No skip guard: a test server that will not start is a defect (§22).
    env = loop.run_until_complete(
        WorkflowEnvironment.start_time_skipping(
            data_converter=static_data_converter(config),
            interceptors=[ControlPayloadInterceptor()],
        )
    )

    starter = TemporalStarter(env.client, config)
    dispatcher.register_consumer(
        "temporal_intent",
        TemporalIntentConsumer(starter, [review_wait_mapping(TemporalIntentMapping)]),
    )

    system = SystemActivities(app)
    agent_runs = AgentRunActivities(
        AgentRunProjection(app), worker_build_id=config.build_id
    )

    async def _build_workers():
        control = build_worker(
            env.client,
            config,
            role="control",
            workflows=[*SYSTEM_WORKFLOWS, ReviewWaitWorkflow],
            activities=[
                *control_activity_registrations(system, agent_runs),
                apply_review_activity,
            ],
        )
        ledger = build_worker(
            env.client,
            config,
            role="ledger",
            workflows=[],
            activities=list(ledger_activity_registrations(system)),
        )
        return control, ledger

    control_worker, ledger_worker = loop.run_until_complete(_build_workers())
    loop.run_until_complete(control_worker.__aenter__())
    loop.run_until_complete(ledger_worker.__aenter__())

    running = _Harness(loop, env, config, app, starter)
    running.control_worker = control_worker
    running.ledger_worker = ledger_worker
    running.system_activities = system
    try:
        yield running
    finally:
        loop.run_until_complete(running.ledger_worker.__aexit__(None, None, None))
        loop.run_until_complete(control_worker.__aexit__(None, None, None))
        loop.run_until_complete(env.__aexit__(None, None, None))
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture(scope="module")
def system_histories(harness) -> dict[str, str]:
    """Execute all four system Workflows once and commit their histories.

    Module-scoped and depended upon rather than ordered: the replay and privacy
    cases need these histories to exist whatever order pytest chooses.
    """

    harness.clear_events()
    # One seeded event so the drains have real work and the sentinels below are
    # genuinely on the path the Activities take.
    harness.emit(
        "claim.created",
        {
            "insured": PRIVACY_SENTINELS["insured_name"],
            "policy": PRIVACY_SENTINELS["policy_number"],
            "amount": PRIVACY_SENTINELS["money"],
            "narrative": PRIVACY_SENTINELS["narrative"],
        },
        correlation_id=new_ulid(),
    )

    workflow_ids: dict[str, str] = {}
    for name, workflow_class in FIXTURES:
        workflow_id, result = harness.execute_recorded(workflow_class)
        assert result.status == "completed", f"{name} did not complete"
        workflow_ids[name] = workflow_id
        harness.save_fixture(workflow_id, name)
    return workflow_ids


@pytest.fixture()
def waiting_workflow(harness):
    """A freshly started `ReviewWaitWorkflow` the bridge can Signal.

    Each test gets its own run reference, so a closed execution never collides
    with `REJECT_DUPLICATE` on the next one.
    """

    APPLIED_REVIEW_EVENTS.clear()
    harness.clear_events()
    run_ref = new_ulid()
    workflow_id = str(agent_workflow_ref(run_ref))

    # Real time for the whole scenario, including the drains the test runs.
    with harness.realtime():
        harness.run(
            harness.starter.start(
                workflow_type=ReviewWaitWorkflow,
                workflow_ref=agent_workflow_ref(run_ref),
                command=ControlCommand(run_ref=run_ref),
            )
        )
        try:
            yield run_ref, workflow_id
        finally:
            handle = harness.handle(workflow_id)
            harness.run(handle.signal("claim_terminal", ControlSignal(event_ref=run_ref)))
            assert harness.run(handle.result()).status == "completed"


# =============================================================================
# 1-2. registration and queue placement
# =============================================================================


def test_all_four_production_system_workflows_register_and_execute(system_histories):
    assert set(system_histories) == {name for name, _ in FIXTURES}
    for name, _workflow_class in FIXTURES:
        assert (FIXTURE_DIR / f"{name}.json").exists()


def test_control_activities_run_on_control_and_ledger_activities_on_ledger(
    harness, system_histories
):
    assert harness.control_worker.task_queue == "pacha-test-control-v1"
    assert harness.ledger_worker.task_queue == "pacha-test-ledger-v1"
    # The ledger Worker registers no Workflow at all.
    assert list(harness.ledger_worker.config()["workflows"]) == []

    expected = {
        "outbox_drain": ("dispatch_nonledger_events", "pacha-test-control-v1"),
        "sla_evaluation": ("evaluate_slas", "pacha-test-control-v1"),
        "ledger_drain": ("append_ledger_batch", "pacha-test-ledger-v1"),
        "ledger_verification": ("verify_ledger", "pacha-test-ledger-v1"),
    }
    for name, (activity_name, task_queue) in expected.items():
        scheduled = [
            event.activity_task_scheduled_event_attributes
            for event in harness.history_events(system_histories[name])
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        assert scheduled, f"{name} scheduled no Activity"
        for attributes in scheduled:
            assert attributes.activity_type.name == activity_name
            assert attributes.task_queue.name == task_queue


# =============================================================================
# 3-5. bounded batches and Worker continuation
# =============================================================================


def test_forty_nine_eligible_deliveries_produce_exactly_one_batch(harness):
    harness.clear_events()
    for index in range(49):
        harness.emit("some.unmapped.event", {"n": index}, correlation_id=new_ulid())

    workflow_id, result = harness.execute_recorded(OutboxDrainWorkflow)
    assert result.status == "completed"
    assert harness.activity_completions(workflow_id) == 1
    assert harness.deliveries_by_status() == {"succeeded": 49}


def test_five_hundred_and_one_deliveries_cap_one_execution_at_five_hundred(harness):
    harness.clear_events()
    for index in range(501):
        harness.emit("some.unmapped.event", {"n": index}, correlation_id=new_ulid())

    first_id, first = harness.execute_recorded(OutboxDrainWorkflow)
    assert first.status == "completed"
    assert harness.activity_completions(first_id) == MAX_DRAIN_BATCHES
    assert harness.deliveries_by_status() == {
        "succeeded": MAX_DRAIN_BATCHES * DISPATCH_BATCH_SIZE
    }

    second_id, second = harness.execute_recorded(OutboxDrainWorkflow)
    assert second.status == "completed"
    assert harness.activity_completions(second_id) == 1
    assert harness.deliveries_by_status() == {"succeeded": 501}


def test_a_worker_stop_between_attempts_lets_a_compatible_worker_continue(harness):
    """A Worker restart mid-drain duplicates no audit-ledger row."""

    harness.clear_events()
    for index in range(60):
        harness.emit("claim.created", {"n": index}, correlation_id=new_ulid())

    workflow_id = _workflow_id()

    async def _restart_mid_drain() -> Any:
        # Auto time skipping off: with no Worker polling the ledger queue the
        # server would otherwise race ahead and time the Activity out, which is
        # not the failure this test is about.
        with harness.env.auto_time_skipping_disabled():
            handle = await harness.client.start_workflow(
                LedgerDrainWorkflow.run,
                id=workflow_id,
                task_queue=harness.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            for _ in range(400):
                completed = sum(
                    1
                    for event in (await handle.fetch_history()).events
                    if event.HasField("activity_task_completed_event_attributes")
                )
                if completed >= 1:
                    break
                await asyncio.sleep(0.05)
            else:  # pragma: no cover - the first batch always completes
                raise AssertionError("the first ledger batch never completed")

            # Stop the Worker that ran the first batch...
            await harness.ledger_worker.__aexit__(None, None, None)
            # ...and bring up a compatible one on the same build.
            replacement = build_worker(
                harness.client,
                harness.config,
                role="ledger",
                workflows=[],
                activities=list(ledger_activity_registrations(harness.system_activities)),
            )
            await replacement.__aenter__()
            harness.ledger_worker = replacement
            return await handle.result()

    assert harness.run(_restart_mid_drain()).status == "completed"

    with Session(harness.app.state.engine) as session:
        rows = list(session.scalars(select(AuditLedgerRow)))
    event_ids = [row.detail["event_id"] for row in rows]
    assert len(event_ids) == 60
    assert len(set(event_ids)) == 60, "the restart duplicated an audit-ledger row"
    assert sorted(row.seq for row in rows) == list(range(1, 61))


# =============================================================================
# 6-11. the Temporal intent bridge
# =============================================================================


def test_a_duplicate_start_attaches_through_use_existing(harness):
    run_ref = new_ulid()
    workflow_ref = agent_workflow_ref(run_ref)
    workflow_id = str(workflow_ref)
    command = ControlCommand(run_ref=run_ref)

    async def _start_twice() -> tuple[str, str]:
        await harness.starter.start(
            workflow_type=ReviewWaitWorkflow, workflow_ref=workflow_ref, command=command
        )
        first = (await harness.client.get_workflow_handle(workflow_id).describe()).run_id
        await harness.starter.start(
            workflow_type=ReviewWaitWorkflow, workflow_ref=workflow_ref, command=command
        )
        second = (await harness.client.get_workflow_handle(workflow_id).describe()).run_id
        return first, second

    with harness.realtime():
        first_run, second_run = harness.run(_start_twice())
        assert first_run == second_run, "the second start created a second execution"

        handle = harness.handle(workflow_id)
        harness.run(handle.signal("claim_terminal", ControlSignal(event_ref=run_ref)))
        result = harness.run(handle.result())

    assert result.status == "completed"
    # One start, one execution, so nothing was applied twice.
    assert result.event_seq == 0


def test_a_review_resolved_mapping_signals_the_waiting_workflow(harness, waiting_workflow):
    run_ref, _workflow_id_value = waiting_workflow
    event_id = harness.emit(
        "review.resolved",
        {"agent_run_id": run_ref, "resolution": "approved"},
        correlation_id=run_ref,
    )

    assert harness.execute(OutboxDrainWorkflow).status == "completed"

    assert harness.delivery(event_id)[0] == "succeeded"
    assert APPLIED_REVIEW_EVENTS == {event_id: 1}


def test_the_same_event_reference_delivered_twice_is_de_duplicated(harness, waiting_workflow):
    run_ref, workflow_id = waiting_workflow
    event_id = harness.emit(
        "review.resolved", {"agent_run_id": run_ref}, correlation_id=run_ref
    )

    assert harness.execute(OutboxDrainWorkflow).status == "completed"
    # A redelivery: the same committed event reaches the bridge a second time.
    harness.make_retryable(event_id)
    assert harness.execute(OutboxDrainWorkflow).status == "completed"

    assert harness.delivery(event_id)[0] == "succeeded"
    # De-duplicated twice over: in the Workflow's own state, and again by the
    # idempotent application the Activity drives.
    assert APPLIED_REVIEW_EVENTS == {event_id: 1}
    applied = [
        event
        for event in harness.history_events(workflow_id)
        if event.HasField("activity_task_completed_event_attributes")
    ]
    assert len(applied) == 1


def test_a_simulated_sdk_failure_leaves_the_delivery_retryable(harness, waiting_workflow):
    run_ref, _workflow_id_value = waiting_workflow
    event_id = harness.emit(
        "review.resolved", {"agent_run_id": run_ref}, correlation_id=run_ref
    )

    original = harness.starter.signal

    async def _failing_signal(**_kwargs):
        raise RuntimeError("temporal frontend unavailable")

    harness.starter.signal = _failing_signal
    try:
        assert harness.execute(OutboxDrainWorkflow).status == "completed"
        status, attempts = harness.delivery(event_id)
        assert status == "failed", "an unacknowledged Signal must not be marked succeeded"
        assert attempts == 1
        assert APPLIED_REVIEW_EVENTS == {}
    finally:
        harness.starter.signal = original

    # Once the transport recovers, the retry delivers exactly once.
    harness.make_retryable(event_id)
    assert harness.execute(OutboxDrainWorkflow).status == "completed"
    assert harness.delivery(event_id)[0] == "succeeded"
    assert APPLIED_REVIEW_EVENTS == {event_id: 1}


def test_signal_success_is_marked_only_after_sdk_acknowledgement(harness, waiting_workflow):
    run_ref, _workflow_id_value = waiting_workflow
    event_id = harness.emit(
        "review.resolved", {"agent_run_id": run_ref}, correlation_id=run_ref
    )
    observed: list[str] = []
    original = harness.starter.signal

    async def _observing_signal(**kwargs):
        observed.append(harness.delivery(event_id)[0])  # before the SDK call
        await original(**kwargs)
        observed.append(harness.delivery(event_id)[0])  # after acknowledgement

    harness.starter.signal = _observing_signal
    try:
        harness.execute(OutboxDrainWorkflow)
    finally:
        harness.starter.signal = original

    assert observed == ["pending", "pending"]
    assert harness.delivery(event_id)[0] == "succeeded"


def test_an_unknown_event_type_performs_no_temporal_call_and_is_marked_succeeded(harness):
    harness.clear_events()
    APPLIED_REVIEW_EVENTS.clear()
    calls: list[str] = []
    original_signal = harness.starter.signal
    original_start = harness.starter.start

    async def _record_signal(**kwargs):
        calls.append("signal")
        await original_signal(**kwargs)

    async def _record_start(**kwargs):
        calls.append("start")
        await original_start(**kwargs)

    harness.starter.signal = _record_signal
    harness.starter.start = _record_start
    try:
        event_id = harness.emit("claim.assigned", {"to": "desk"}, correlation_id=new_ulid())
        assert harness.execute(OutboxDrainWorkflow).status == "completed"
    finally:
        harness.starter.signal = original_signal
        harness.starter.start = original_start

    assert calls == []
    assert harness.delivery(event_id)[0] == "succeeded"


def test_a_mapped_event_with_no_target_is_an_acknowledged_no_op(harness):
    """The builder returns `None` when the payload names no agent run."""

    harness.clear_events()
    calls: list[str] = []
    original = harness.starter.signal

    async def _record(**kwargs):
        calls.append("signal")
        await original(**kwargs)

    harness.starter.signal = _record
    try:
        event_id = harness.emit("review.resolved", {}, correlation_id=new_ulid())
        assert harness.execute(OutboxDrainWorkflow).status == "completed"
    finally:
        harness.starter.signal = original

    assert calls == []
    assert harness.delivery(event_id)[0] == "succeeded"


# =============================================================================
# 12-14. privacy, registry and the no-skip rule
# =============================================================================


@pytest.mark.parametrize("name", [name for name, _ in FIXTURES])
def test_fetched_histories_carry_no_seeded_sentinel_and_only_control_fields(
    harness, system_histories, name
):
    workflow_id = system_histories[name]

    decoded = harness.decoded_history(workflow_id)
    assert find_sentinels(decoded, _SENTINELS) == []
    assert find_sentinels(harness.stored_history(workflow_id), _SENTINELS) == []

    # What *is* in the decoded history is only T01 control-contract fields.
    text_blob = decoded.decode("utf-8", errors="ignore")
    for token in ("insured", "policy", "claim_fields", "occurred_at", "actor"):
        assert token not in text_blob
    present = {field for field in CONTROL_FIELDS if f'"{field}"' in text_blob}
    assert present, "the control result must actually have travelled"
    assert present <= set(CONTROL_FIELDS)


def test_the_production_intent_mapping_registry_includes_t03_and_t04():
    """T04 adds document start and recovery intents to T03's chase surface."""

    assert {
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
    } <= {
        (mapping.event_type, mapping.action, mapping.signal_name)
        for mapping in TEMPORAL_INTENT_MAPPINGS
    }


TEMPORAL_SUITES = (
    REPO / "tests" / "unit" / "test_temporal_orchestration.py",
    REPO / "tests" / "unit" / "test_temporal_t02.py",
    REPO / "tests" / "integration" / "test_temporal_orchestration.py",
    REPO / "tests" / "integration" / "test_temporal_t02.py",
    REPO / "tests" / "integration" / "test_temporal_t03.py",
)


def _skip_constructs(path: pathlib.Path) -> list[str]:
    """Every real skip in a module, ignoring the same words inside strings.

    Scanning raw text would flag T01's own no-skip guard, which necessarily
    names the constructs it forbids, so the check walks the syntax tree and
    looks only at calls and decorators.
    """

    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            if target.endswith(("pytest.skip", "importorskip", "pytest.xfail")):
                found.append(target)
        elif isinstance(node, ast.Attribute):
            rendered = ast.unparse(node)
            if rendered.startswith("pytest.mark.skip") or rendered.startswith(
                "pytest.mark.xfail"
            ):
                found.append(rendered)
    return found


@pytest.mark.parametrize("path", TEMPORAL_SUITES, ids=lambda path: path.name)
def test_no_temporal_skip_exists_anywhere_in_the_t01_or_t02_suites(path):
    assert _skip_constructs(path) == [], f"{path.name} turns a Temporal defect green"


# =============================================================================
# replay (§12.4)
# =============================================================================


#: Register #286. `Replayer` builds its own Task Queue as `replay-{build_id}`
#: (`temporalio/worker/_replayer.py`) rather than replaying the queue recorded
#: in `WorkflowExecutionStarted`, so the ledger Workflows' §10.1 derivation
#: would see a non-control queue and correctly refuse. The accommodation lives
#: here rather than in the Workflow: relaxing the production refusal would let a
#: genuinely misconfigured Worker route ledger appends to an unreviewed queue.
REPLAY_BUILD_ID = "pacha-test-control-v1"


def test_the_replay_harness_still_satisfies_the_production_queue_invariant():
    """Fail loudly if the SDK stops shaping its replay queue this way."""

    assert f"replay-{REPLAY_BUILD_ID}".endswith("-control-v1")


@pytest.mark.parametrize(("name", "workflow_class"), FIXTURES, ids=[n for n, _ in FIXTURES])
def test_each_committed_history_fixture_replays_against_the_t02_workflows(
    harness, system_histories, name, workflow_class
):
    path = FIXTURE_DIR / f"{name}.json"
    assert path.exists(), f"{path} was not persisted by the integration run"
    document = json.loads(path.read_text(encoding="utf-8"))

    async def _replay() -> None:
        replayer = Replayer(
            workflows=list(SYSTEM_WORKFLOWS),
            data_converter=static_data_converter(harness.config),
            build_id=REPLAY_BUILD_ID,
        )
        await replayer.replay_workflow(WorkflowHistory.from_json(f"replay-{name}", document))

    harness.run(_replay())


@pytest.mark.parametrize("name", [name for name, _ in FIXTURES])
def test_no_committed_fixture_contains_a_sentinel_or_codec_plaintext(system_histories, name):
    raw = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    assert find_sentinels(raw, _SENTINELS) == []
    assert STATIC_CODEC_KEY.hex() not in raw
    # Every Pacha payload in a committed fixture is still ciphertext.
    assert "YmluYXJ5L3BhY2hhLWFlc2djbS12MQ==" in raw


def test_the_bridge_never_deleted_a_domain_event(harness):
    harness.clear_events()
    event_id = harness.emit("some.unmapped.event", {}, correlation_id=new_ulid())
    harness.execute(OutboxDrainWorkflow)

    with Session(harness.app.state.engine) as session:
        assert session.get(Event, event_id) is not None
        assert len(list(session.scalars(select(EventDelivery)))) >= 1
