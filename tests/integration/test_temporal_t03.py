"""T03 acceptance against a real time-skipping Temporal server."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from assessment_agent import (
    AssessmentActivities,
    assessment_control_activity_registrations,
    assessment_effect_activity_registrations,
)
from assessment_agent.workflows import AssessmentWorkflow
from chase_agent import chase_activity_registrations
from claim_core.service import new_ulid
from doc_intel.activities import (
    DocumentIntelligenceActivities,
    docintel_activity_registrations,
)
from doc_intel.workflows import DocumentIntelligenceWorkflow
from intake_agent import (
    IntakeActivities,
    intake_control_activity_registrations,
    intake_effect_activity_registrations,
)
from intake_agent.workflows import IntakeWorkflow
from orchestration.activities import SystemActivities
from orchestration.chase_workflow import DocumentChaseWorkflow
from orchestration.contracts import (
    ControlCommand,
    ControlPayloadInterceptor,
    ControlResult,
    ControlSignal,
)
from orchestration.history import decoded_history_blob, history_blob
from orchestration.ids import agent_workflow_ref, chase_workflow_ref
from orchestration.starter import (
    TEMPORAL_INTENT_MAPPINGS,
    TemporalIntentConsumer,
    TemporalStarter,
)
from orchestration.worker import build_worker
from orchestration.workflows import OutboxDrainWorkflow
from support.temporal import local_config, plain_client_for, static_data_converter


def _acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance/test_packet_15_chase_agent.py"
    spec = importlib.util.spec_from_file_location("t03_integration_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P15 = _acceptance_module()


def _aware(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def test_acceptance_driver_drains_events_emitted_by_a_slow_chase_activity(
    tmp_path,
    temporal_chase,
    monkeypatch,
):
    domain = P15._build(
        tmp_path,
        "temporal-t03-slow-activity",
        model=P15._intimation_model(),
    )
    checklist = domain.app.state.chase_agent.checklist
    ensure_initial_request = checklist.ensure_initial_request

    def delayed_initial_request(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        time.sleep(0.75)
        return ensure_initial_request(*args, **kwargs)

    monkeypatch.setattr(
        checklist,
        "ensure_initial_request",
        delayed_initial_request,
    )
    temporal_chase(domain)

    claim_id = P15._to_checklist(domain)

    clocks = P15._rows(
        domain.app,
        "SELECT id FROM sla_clocks WHERE claim_id = :claim_id "
        "AND definition_id = 'doc_item_age' AND stopped_at IS NULL",
        claim_id=claim_id,
    )
    assert len(clocks) == 7


class _Harness:
    def __init__(self, loop, temporal, config, domain) -> None:
        self.loop = loop
        self.temporal = temporal
        self.config = config
        self.domain = domain

    @property
    def client(self):
        return self.temporal.client

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def realtime(self):
        return self.temporal.auto_time_skipping_disabled()

    def drain(self) -> ControlResult:
        async def _drain() -> ControlResult:
            result = ControlResult(status="completed")
            for _ in range(3):
                result = await self.client.execute_workflow(
                    OutboxDrainWorkflow.run,
                    id=str(agent_workflow_ref(new_ulid())),
                    task_queue=self.config.task_queue("control"),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
                await asyncio.sleep(0.5)
            return result

        with self.realtime():
            return self.run(_drain())

    def settle(self) -> None:
        async def _settle() -> None:
            for _ in range(100):
                await asyncio.sleep(0.02)

        with self.realtime():
            self.run(_settle())

    def advance(self, duration: timedelta) -> None:
        self.domain.clock.advance_to(self.domain.clock.now + duration)
        self.run(self.temporal.sleep(duration))
        self.settle()

    def state(self, workflow_id: str) -> ControlResult:
        async def _query() -> ControlResult:
            handle = self.client.get_workflow_handle(workflow_id)
            for _ in range(100):
                try:
                    return await handle.query("state", result_type=ControlResult)
                except Exception:  # Workflow may not have completed its first Task yet.
                    await asyncio.sleep(0.02)
            raise AssertionError("document chase state Query did not become available")

        with self.realtime():
            return self.run(_query())

    def wait_state(self, workflow_id: str, expected: str) -> ControlResult:
        async def _wait() -> ControlResult:
            handle = self.client.get_workflow_handle(workflow_id)
            observed: ControlResult | None = None
            for _ in range(250):
                try:
                    observed = await handle.query("state", result_type=ControlResult)
                except Exception:
                    await asyncio.sleep(0.02)
                    continue
                if observed.status == expected:
                    return observed
                await asyncio.sleep(0.02)
            raise AssertionError(
                f"document chase stayed at {getattr(observed, 'status', None)!r}; "
                f"expected {expected!r}"
            )

        with self.realtime():
            return self.run(_wait())

    def decoded_history(self, workflow_id: str) -> bytes:
        async def _fetch() -> bytes:
            handle = self.client.get_workflow_handle(workflow_id)
            return await decoded_history_blob(
                await handle.fetch_history(),
                self.client.data_converter.payload_codec,
            )

        return self.run(_fetch())

    def stored_history(self, workflow_id: str) -> bytes:
        async def _fetch() -> bytes:
            plain = await plain_client_for(self.client)
            history = await plain.get_workflow_handle(workflow_id).fetch_history()
            return history_blob(history)

        return self.run(_fetch())


@pytest.fixture
def harness(tmp_path):
    config = local_config()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    temporal = loop.run_until_complete(
        WorkflowEnvironment.start_time_skipping(
            data_converter=static_data_converter(config),
            interceptors=[ControlPayloadInterceptor()],
        )
    )
    temporal_now = loop.run_until_complete(temporal.get_current_time())

    domain = P15._build(tmp_path, "temporal-t03", model=P15._intimation_model())
    # Keep cadence assertions independent of the test server's wall-clock
    # weekday. Monday 09:00 EAT is an exact open send-window boundary.
    aligned = temporal_now.astimezone(UTC).replace(
        hour=6,
        minute=0,
        second=0,
        microsecond=0,
    )
    aligned += timedelta(days=(-aligned.weekday()) % 7)
    if aligned <= temporal_now:
        aligned += timedelta(days=7)
    if aligned > temporal_now:
        loop.run_until_complete(temporal.sleep(aligned - temporal_now))
    domain.clock.advance_to(aligned)

    starter = TemporalStarter(temporal.client, config)
    domain.app.state.dispatcher.register_consumer(
        "temporal_intent",
        TemporalIntentConsumer(starter, TEMPORAL_INTENT_MAPPINGS),
    )
    system = SystemActivities(domain.app)
    chase = domain.app.state.chase_agent.temporal_activities(
        worker_build_id=config.build_id
    )
    docintel = DocumentIntelligenceActivities(
        domain.app.state.doc_intel,
        worker_build_id=config.build_id,
    )
    intake = IntakeActivities(domain.app)
    assessment = AssessmentActivities(domain.app)

    async def _workers():
        control_worker = build_worker(
            temporal.client,
            config,
            role="control",
            workflows=[
                OutboxDrainWorkflow,
                DocumentChaseWorkflow,
                DocumentIntelligenceWorkflow,
                IntakeWorkflow,
                AssessmentWorkflow,
            ],
            activities=[
                system.dispatch_nonledger_events,
                *chase_activity_registrations(chase),
                *intake_control_activity_registrations(intake),
                *assessment_control_activity_registrations(assessment),
            ],
        )
        docintel_worker = build_worker(
            temporal.client,
            config,
            role="docintel",
            activities=docintel_activity_registrations(docintel),
        )
        effects_worker = build_worker(
            temporal.client,
            config,
            role="effects",
            activities=[
                *intake_effect_activity_registrations(intake),
                *assessment_effect_activity_registrations(assessment),
            ],
        )
        return control_worker, docintel_worker, effects_worker

    worker, docintel_worker, effects_worker = loop.run_until_complete(_workers())
    loop.run_until_complete(worker.__aenter__())
    loop.run_until_complete(docintel_worker.__aenter__())
    loop.run_until_complete(effects_worker.__aenter__())
    running = _Harness(loop, temporal, config, domain)
    domain.app.state.temporal_chase_driver = running
    claim_id = P15._to_checklist(domain)
    running.claim_id = claim_id
    try:
        yield running
    finally:
        delattr(domain.app.state, "temporal_chase_driver")
        loop.run_until_complete(effects_worker.__aexit__(None, None, None))
        loop.run_until_complete(docintel_worker.__aexit__(None, None, None))
        loop.run_until_complete(worker.__aexit__(None, None, None))
        loop.run_until_complete(temporal.__aexit__(None, None, None))
        asyncio.set_event_loop(None)
        loop.close()


def _checklist_context(harness: _Harness) -> tuple[str, str, str]:
    checklist_id = P15._checklists(harness.domain.app, harness.claim_id)[0]["id"]
    with harness.domain.app.state.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, workflow_id FROM agent_runs "
                "WHERE workflow_id = :workflow_id"
            ),
            {"workflow_id": str(chase_workflow_ref(checklist_id))},
        ).one()
    return checklist_id, str(row[0]), str(row[1])


def _emit_inbound(harness: _Harness) -> str:
    app = harness.domain.app
    communication, _created = app.state.claim_service.record_inbound_communication(
        graph_message_id=f"t03-inbound-{new_ulid()}",
        claim_id=harness.claim_id,
        thread_id="conv-intake-1",
        from_addr=P15.BROKER_ADDR,
        to_addrs=[P15.SELF_ADDRESS],
        subject="Documents are being collected",
        body_text="We are gathering the remaining documents",
    )
    with Session(app.state.engine) as session, session.begin():
        event = app.state.record_event(
            session,
            claim_id=harness.claim_id,
            event_type="INBOUND_ATTACHED",
            payload={
                "graph_message_id": communication.graph_message_id,
                "match": "thread",
                "terminal": False,
            },
            actor="agent:intake",
            correlation_id=new_ulid(),
        )
        return event.id


def _terminal_claim(harness: _Harness) -> None:
    app = harness.domain.app
    with Session(app.state.engine) as session, session.begin():
        session.execute(
            text("UPDATE claims SET status = 'DECLINED' WHERE id = :claim_id"),
            {"claim_id": harness.claim_id},
        )
        app.state.record_event(
            session,
            claim_id=harness.claim_id,
            event_type="claim.status_changed",
            payload={"from": "TRIAGED", "to": "DECLINED"},
            actor=P15.OFFICER_A,
            correlation_id=new_ulid(),
        )


def test_complete_document_chase_uses_durable_time_signals_and_review(harness):
    checklist_id, run_ref, workflow_id = _checklist_context(harness)

    # Start intent is delivered through the real outbox Activity. The initial
    # request is the first governed Activity, not checklist-instantiation code.
    assert harness.drain().status == "completed"
    harness.settle()
    assert harness.drain().status == "completed"
    assert len(P15._drafts(harness.domain.app, harness.claim_id, "intake.doc_request")) == 1
    assert all(
        item["state"] == "requested"
        for item in P15._chase_items(harness.domain.app, harness.claim_id).values()
    )

    # Duplicate delivery attaches to the same open execution.
    async def _duplicate() -> tuple[str, str]:
        handle = harness.client.get_workflow_handle(workflow_id)
        before = (await handle.describe()).run_id
        event = P15._events(
            harness.domain.app,
            "chase.workflow_requested",
            harness.claim_id,
        )[0]
        await TemporalStarter(harness.client, harness.config).start(
            workflow_type=DocumentChaseWorkflow,
            workflow_ref=chase_workflow_ref(checklist_id),
            command=ControlCommand(
                run_ref=run_ref,
                claim_ref=harness.claim_id,
                trigger_event_ref=event["id"],
                event_ref=event["id"],
            ),
        )
        after = (await handle.describe()).run_id
        return before, after

    with harness.realtime():
        before, after = harness.run(_duplicate())
        assert before == after

    # A syntactically valid but unauthorised human event is rejected by the
    # Activity without killing the live Workflow.
    service = harness.domain.app.state.chase_agent.checklist
    with service.sessions.begin() as session:
        unauthorised_ref = service.emit_event(
            session,
            claim_id=harness.claim_id,
            event_type="chase.item_snoozed",
            payload={"checklist_id": checklist_id},
            actor="user:01HCHASEUNKNOWN00000000AAAA",
        )
    with harness.realtime():
        harness.run(
            harness.client.get_workflow_handle(workflow_id).signal(
                "snooze_changed",
                ControlSignal(event_ref=unauthorised_ref),
            )
        )
    harness.settle()
    assert harness.state(workflow_id).status == "running"

    # An inbound reply Signal defers every requested item for 48 hours.
    harness.advance(timedelta(days=2))
    _emit_inbound(harness)
    harness.drain()
    harness.drain()
    deferred = P15._chase_items(harness.domain.app, harness.claim_id)
    floor = harness.domain.clock.now + timedelta(hours=48)
    assert all(
        _aware(item["next_reminder_at"]) >= floor
        for item in deferred.values()
        if item["state"] in {"requested", "rejected"}
    )

    # A real document-domain Signal is applied exactly once.
    item_id = next(iter(deferred.values()))["id"]
    response = harness.domain.client.post(
        f"/chase/items/{item_id}/waive",
        json={"reason": "Synthetic Temporal acceptance evidence"},
        headers=P15._h(P15.OFFICER_A),
    )
    assert response.status_code == 200
    harness.drain()
    assert harness.state(workflow_id).status == "running"

    # Thirty days of durable time produces only the pack cadence reminders.
    harness.advance(timedelta(days=30))
    harness.drain()
    assert len(P15._drafts(harness.domain.app, harness.claim_id, "chase.reminder")) == 5

    # The sixth reminder reaches the hard cap and creates one human wait.
    harness.advance(timedelta(days=8))
    harness.drain()
    exhausted = P15._items(
        harness.domain.app,
        claim_id=harness.claim_id,
        type="EXCEPTION",
        subtype="chase_exhausted",
    )
    assert len(exhausted) == 1
    assert harness.wait_state(workflow_id, "awaiting_review").status == "awaiting_review"

    resolved = P15._resolve(
        harness.domain,
        exhausted[0]["id"],
        P15.OFFICER_A,
        action="approve",
        schema_version="EXCEPTION@1",
        payload={
            "capability_id": "chase.checklist",
            "diff": P15._diff(),
        },
    )
    assert resolved.status_code == 200
    harness.drain()
    harness.drain()
    assert harness.wait_state(workflow_id, "running").status == "running"
    harness.settle()
    continued = harness.state(workflow_id)
    assert continued.step_id == "chase_wait"
    assert continued.wake_at_epoch_ms is None
    assert len(P15._drafts(harness.domain.app, harness.claim_id, "chase.reminder")) == 6

    # Terminal claim state wins over the stale timer and closes the Workflow
    # without a seventh reminder.
    _terminal_claim(harness)
    harness.drain()
    harness.drain()
    result = harness.run(
        harness.client.get_workflow_handle(
            workflow_id,
            result_type=ControlResult,
        ).result()
    )
    assert result.status == "cancelled"
    assert len(P15._drafts(harness.domain.app, harness.claim_id, "chase.reminder")) == 6

    sentinels = (
        "Jane Wanjiku",
        "KBX 123A",
        P15.BROKER_ADDR,
        "Documents are being collected",
        "Synthetic Temporal acceptance evidence",
    )
    decoded = harness.decoded_history(workflow_id)
    stored = harness.stored_history(workflow_id)
    assert all(value.encode() not in decoded for value in sentinels)
    assert all(value.encode() not in stored for value in sentinels)

    async def _replay() -> None:
        history = await harness.client.get_workflow_handle(workflow_id).fetch_history()
        replayer = Replayer(
            workflows=[DocumentChaseWorkflow],
            data_converter=static_data_converter(harness.config),
            build_id=harness.config.build_id,
        )
        await replayer.replay_workflow(history)

    harness.run(_replay())


def test_lost_governed_write_becomes_resumable_uncertain_write(
    harness,
    monkeypatch,
):
    checklist_id, _run_ref, workflow_id = _checklist_context(harness)
    harness.drain()
    harness.settle()
    harness.drain()
    comms = harness.domain.app.state.agent_runtime.comms
    original_send = comms.send

    def lose_outcome(**_kwargs):
        raise RuntimeError("synthetic post-schedule outcome loss")

    monkeypatch.setattr(comms, "send", lose_outcome)
    harness.advance(timedelta(days=4))
    assert harness.wait_state(workflow_id, "awaiting_review").status == "awaiting_review"
    harness.drain()
    uncertain = P15._items(
        harness.domain.app,
        claim_id=harness.claim_id,
        type="EXCEPTION",
        subtype="uncertain_write",
    )
    assert len(uncertain) == 1
    payload = uncertain[0]["payload"]
    assert payload["write_id"] == f"chase:{checklist_id.lower()}:1"
    assert set(("facts", "risk", "recommendation", "resolution_schema")) <= set(
        payload
    )
    assert P15._drafts(
        harness.domain.app,
        harness.claim_id,
        "chase.reminder",
    ) == []

    monkeypatch.setattr(comms, "send", original_send)
    resolved = P15._resolve(
        harness.domain,
        uncertain[0]["id"],
        P15.OFFICER_A,
        action="approve",
        schema_version="EXCEPTION@1",
        payload={
            "capability_id": "chase.checklist",
            "diff": P15._diff(),
        },
    )
    assert resolved.status_code == 200
    harness.drain()
    harness.drain()
    assert harness.wait_state(workflow_id, "running").status == "running"
    harness.settle()
    harness.drain()
    reminders = P15._drafts(
        harness.domain.app,
        harness.claim_id,
        "chase.reminder",
    )
    assert len(reminders) == 1, {
        "state": harness.state(workflow_id),
        "items": P15._chase_items(harness.domain.app, harness.claim_id),
        "reviews": P15._events(
            harness.domain.app,
            "review.created",
            harness.claim_id,
        ),
    }


def test_chatty_chase_continues_as_new_with_persisted_high_water(harness):
    checklist_id, _run_ref, workflow_id = _checklist_context(harness)
    harness.drain()
    harness.settle()
    harness.drain()
    service = harness.domain.app.state.chase_agent.checklist
    event_refs = []
    with service.sessions.begin() as session:
        for _ in range(64):
            event_refs.append(
                service.emit_event(
                    session,
                    claim_id=harness.claim_id,
                    event_type="chase.item_snoozed",
                    payload={"checklist_id": checklist_id},
                    actor=P15.OFFICER_A,
                )
            )

    async def _drive() -> tuple[str, str]:
        handle = harness.client.get_workflow_handle(workflow_id)
        before = (await handle.describe()).run_id
        for event_ref in event_refs:
            await handle.signal(
                "snooze_changed",
                ControlSignal(event_ref=event_ref),
            )
        for _ in range(300):
            after = (await handle.describe()).run_id
            if after != before:
                return before, after
            await asyncio.sleep(0.02)
        raise AssertionError("chatty chase did not Continue-As-New")

    with harness.realtime():
        before, after = harness.run(_drive())
    assert before != after
    with harness.domain.app.state.engine.connect() as connection:
        steps = connection.execute(
            text(
                "SELECT steps FROM agent_runs "
                "WHERE workflow_id = :workflow_id"
            ),
            {"workflow_id": workflow_id},
        ).scalar_one()
    if isinstance(steps, str):
        steps = json.loads(steps)
    apply_step = next(
        step for step in steps if step["step_id"] == "chase_apply_event"
    )
    assert apply_step["event_seq"] > 0
    assert harness.wait_state(workflow_id, "running").status == "running"
