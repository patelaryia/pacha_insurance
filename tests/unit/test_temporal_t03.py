"""T03 unit coverage for the production document-chase Temporal boundary."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from temporalio.common import VersioningBehavior

from chase_agent import chase_activity_registrations
from claim_core import new_ulid
from claim_core.ledger import ACTION_MAP
from orchestration.chase_workflow import CHASE_WORKFLOWS, DocumentChaseWorkflow
from orchestration.contracts import ControlCommand
from orchestration.starter import TEMPORAL_INTENT_MAPPINGS


def _acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance/test_packet_15_chase_agent.py"
    spec = importlib.util.spec_from_file_location("t03_packet15_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P15 = _acceptance_module()


def _prepared(env, claim_id):
    checklist_id = P15._checklists(env.app, claim_id)[0]["id"]
    with env.app.state.engine.connect() as connection:
        run = connection.execute(
            text(
                "SELECT id, status, workflow_id, workflow_type, steps "
                "FROM agent_runs WHERE workflow_id = :workflow_id"
            ),
            {"workflow_id": f"pacha.chase.{checklist_id}"},
        ).mappings().one()
    return checklist_id, run


def _activity_context(env, claim_id):
    checklist_id, run = _prepared(env, claim_id)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_runs SET status = 'running' WHERE id = :run_id"),
            {"run_id": run["id"]},
        )
    activities = env.app.state.chase_agent.temporal_activities(
        worker_build_id="a" * 40
    )
    command = ControlCommand(
        run_ref=run["id"],
        claim_ref=claim_id,
        checklist_ref=checklist_id,
    )
    return checklist_id, run, activities, command


def _initialise(env, claim_id):
    checklist_id, run, activities, command = _activity_context(env, claim_id)
    result = activities._governed_send(  # noqa: SLF001 - Activity engine unit seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:0",
            step_id="chase_initial_request",
        )
    )
    P15._drain(env.app)
    assert result.status == "running"
    return checklist_id, run, activities, command


def test_checklist_instantiation_atomically_prepares_the_t03_start(tmp_path):
    env = P15._build(tmp_path, "t03-prepare", model=P15._intimation_model())
    env.clock.advance_to(P15.T0 - timedelta(days=1))
    claim_id = P15._to_checklist(env)
    checklist_id, run = _prepared(env, claim_id)

    assert run["status"] == "pending"
    assert run["workflow_id"] == f"pacha.chase.{checklist_id}"
    assert run["workflow_type"] == "DocumentChaseWorkflow"
    steps = json.loads(run["steps"]) if isinstance(run["steps"], str) else run["steps"]
    assert [step["step_id"] for step in steps] == [
        "chase_record_start",
        "chase_load_state",
        "chase_initial_request",
        "chase_wait",
        "chase_apply_event",
        "chase_reminder",
        "chase_exhausted",
        "chase_terminal",
    ]
    requested = P15._events(env.app, "chase.workflow_requested", claim_id)
    assert len(requested) == 1
    assert requested[0]["payload"]["checklist_id"] == checklist_id
    with env.app.state.engine.connect() as connection:
        correlation_id = connection.execute(
            text("SELECT correlation_id FROM events WHERE id = :event_id"),
            {"event_id": requested[0]["id"]},
        ).scalar_one()
    assert correlation_id == run["id"]
    assert P15._drafts(env.app, claim_id, "intake.doc_request") == []


def test_load_state_is_control_only_and_send_revalidates_the_checklist(tmp_path):
    env = P15._build(tmp_path, "t03-state", model=P15._intimation_model())
    env.clock.advance_to(P15.T0 - timedelta(days=1))
    claim_id = P15._to_checklist(env)
    env.clock.advance_to(P15.T0)
    checklist_id, run = _prepared(env, claim_id)
    activities = env.app.state.chase_agent.temporal_activities(
        worker_build_id="a" * 40
    )
    command = ControlCommand(
        run_ref=run["id"],
        claim_ref=claim_id,
        checklist_ref=checklist_id,
    )

    state = activities._load(command)  # noqa: SLF001 - Activity engine unit seam
    assert state.as_control_mapping().keys() <= {
        "status",
        "step_id",
        "wake_at_epoch_ms",
        "payload_hash",
        "event_ref",
        "attempt_no",
    }
    assert state.step_id == "chase_initial_request"

    sent = activities._governed_send(  # noqa: SLF001 - Activity engine unit seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:0",
        )
    )
    P15._drain(env.app)
    assert sent.status == "running"
    assert len(P15._drafts(env.app, claim_id, "intake.doc_request")) == 1
    assert all(
        item["state"] == "requested"
        for item in P15._chase_items(env.app, claim_id).values()
    )

    env.app.state.chase_agent.checklist.cancel_claim(claim_id)
    stale = activities._governed_send(  # noqa: SLF001 - Activity engine unit seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:1",
        )
    )
    assert stale.status == "cancelled"
    assert P15._drafts(env.app, claim_id, "chase.reminder") == []


def test_apply_event_rejects_an_unauthorised_human_wake(tmp_path):
    env = P15._build(tmp_path, "t03-event-auth", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run = _prepared(env, claim_id)
    service = env.app.state.chase_agent.checklist
    with service.sessions.begin() as session:
        event_ref = service._emit(  # noqa: SLF001 - adversarial Activity boundary
            session,
            claim_id=claim_id,
            event_type="chase.item_snoozed",
            payload={"checklist_id": checklist_id},
            actor="user:01HCHASEUNKNOWN00000000AAAA",
        )
    activities = env.app.state.chase_agent.temporal_activities(
        worker_build_id="a" * 40
    )

    with pytest.raises(ValueError, match="actor is not authorised"):
        activities._apply_event(  # noqa: SLF001 - Activity engine unit seam
            ControlCommand(
                run_ref=run["id"],
                claim_ref=claim_id,
                checklist_ref=checklist_id,
                event_ref=event_ref,
            )
        )


def test_stale_reminder_timer_reloads_after_snooze_race(tmp_path):
    env = P15._build(tmp_path, "t03-stale-race", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, command = _initialise(env, claim_id)
    env.clock.advance_to(P15.T0 + timedelta(days=4))
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :now, reminder_count = 0 "
                "WHERE checklist_id = :checklist_id"
            ),
            {"now": env.clock.now, "checklist_id": checklist_id},
        )
    loaded = activities._load(command)  # noqa: SLF001 - Activity engine unit seam
    assert loaded.step_id == "chase_reminder"
    assert loaded.attempt_no == 1

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET snooze_until = :later "
                "WHERE checklist_id = :checklist_id"
            ),
            {
                "later": env.clock.now + timedelta(days=1),
                "checklist_id": checklist_id,
            },
        )
    stale = activities._governed_send(  # noqa: SLF001 - race seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:1",
            step_id="chase_reminder",
        )
    )
    assert stale.status == "running"
    assert stale.step_id == "chase_load_state"
    assert P15._drafts(env.app, claim_id, "chase.reminder") == []


def test_existing_staged_initial_request_reconciles_pending_items(tmp_path):
    env = P15._build(tmp_path, "t03-initial-reconcile", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, _command = _activity_context(env, claim_id)
    requester = env.app.state.chase_agent.checklist.requester(claim_id)[0]
    assert requester is not None
    with env.app.state.chase_agent.checklist.sessions.begin() as session:
        env.app.state.chase_agent.checklist.emit_event(
            session,
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "review_id": new_ulid(),
                "type": "DRAFT_RELEASE",
                "capability_id": "intake.doc_request",
                "action": {
                    "type": "communication.send",
                    "payload": {
                        "template_id": "T-06",
                        "to_party_ids": [requester],
                    },
                },
            },
        )

    result = activities._governed_send(  # noqa: SLF001 - recovery seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:0",
            step_id="chase_initial_request",
        )
    )
    assert result.status == "running"
    assert all(
        item["state"] == "requested" and item["requested_at"] is not None
        for item in P15._chase_items(env.app, claim_id).values()
    )
    assert activities._load(  # noqa: SLF001 - Activity engine unit seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
        )
    ).step_id != "chase_initial_request"


@pytest.mark.parametrize(
    ("reminder_count", "expected_step", "expected_attempt"),
    [
        (5, "chase_reminder", 6),
        (6, "chase_exhausted", None),
    ],
)
def test_reminder_cap_boundary_is_exact(
    tmp_path,
    reminder_count,
    expected_step,
    expected_attempt,
):
    env = P15._build(
        tmp_path,
        f"t03-cap-{reminder_count}",
        model=P15._intimation_model(),
    )
    claim_id = P15._to_checklist(env)
    checklist_id, _run, activities, command = _initialise(env, claim_id)
    env.clock.advance_to(P15.T0 + timedelta(days=40))
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :now, "
                "reminder_count = :reminder_count "
                "WHERE checklist_id = :checklist_id"
            ),
            {
                "now": env.clock.now,
                "reminder_count": reminder_count,
                "checklist_id": checklist_id,
            },
        )
    state = activities._load(command)  # noqa: SLF001 - boundary seam
    assert state.step_id == expected_step
    assert state.attempt_no == expected_attempt


def test_future_cap_due_retains_the_exhaustion_timer(tmp_path):
    env = P15._build(
        tmp_path,
        "t03-cap-future-wake",
        model=P15._intimation_model(),
    )
    claim_id = P15._to_checklist(env)
    checklist_id, _run, activities, command = _initialise(env, claim_id)
    due_at = env.clock.now + timedelta(days=7)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :due_at, "
                "reminder_count = 6 WHERE checklist_id = :checklist_id"
            ),
            {"due_at": due_at, "checklist_id": checklist_id},
        )

    waiting = activities._load(command)  # noqa: SLF001 - timer boundary seam
    assert waiting.step_id == "chase_wait"
    assert waiting.wake_at_epoch_ms == int(due_at.timestamp() * 1000)
    assert waiting.attempt_no is None

    env.clock.advance_to(due_at)
    exhausted = activities._load(command)  # noqa: SLF001 - timer boundary seam
    assert exhausted.step_id == "chase_exhausted"
    assert exhausted.attempt_no is None
    assert len(P15._drafts(env.app, claim_id, "chase.reminder")) == 0


def test_cc_insured_starts_at_reminder_two_exactly(tmp_path):
    env = P15._build(tmp_path, "t03-cc-boundary", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, _command = _initialise(env, claim_id)
    insured_id = P15._rows(
        env.app,
        "SELECT id FROM parties WHERE claim_id = :claim_id AND role = 'insured'",
        claim_id=claim_id,
    )[0]["id"]
    env.clock.advance_to(P15.T0 + timedelta(days=4))

    recipients = []
    for reminder_count in (0, 1):
        with env.app.state.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE chase_items SET next_reminder_at = :now, "
                    "reminder_count = :reminder_count "
                    "WHERE checklist_id = :checklist_id"
                ),
                {
                    "now": env.clock.now,
                    "reminder_count": reminder_count,
                    "checklist_id": checklist_id,
                },
            )
        activities._governed_send(  # noqa: SLF001 - boundary seam
            ControlCommand(
                run_ref=run["id"],
                claim_ref=claim_id,
                checklist_ref=checklist_id,
                write_id=(
                    f"chase:{checklist_id.lower()}:{reminder_count + 1}"
                ),
                step_id="chase_reminder",
            )
        )
        P15._drain(env.app)
        draft = P15._drafts(env.app, claim_id, "chase.reminder")[-1]
        recipients.append(draft["payload"]["action"]["payload"]["to_party_ids"])

    assert insured_id not in recipients[0]
    assert insured_id in recipients[1]


def test_next_send_window_observes_sunday_and_exact_open_boundary(tmp_path):
    env = P15._build(tmp_path, "t03-send-window", model=P15._intimation_model())
    comms = env.app.state.agent_runtime.comms
    sunday = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    monday_open = datetime(2026, 7, 27, 5, 0, tzinfo=UTC)
    assert comms.next_send_window(sunday) == monday_open
    assert comms.next_send_window(monday_open) == monday_open


def test_requester_missing_waits_for_review_without_ending_run(tmp_path):
    env = P15._build(tmp_path, "t03-requester-missing", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, _command = _activity_context(env, claim_id)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE parties SET meta = :meta WHERE claim_id = :claim_id"),
            {"meta": "{}", "claim_id": claim_id},
        )
    result = activities._governed_send(  # noqa: SLF001 - negative seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:0",
            step_id="chase_initial_request",
        )
    )
    assert result.status == "awaiting_review"
    assert P15._checklists(env.app, claim_id)[0]["status"] == "open"
    exception = P15._events(env.app, "review.created", claim_id)[-1]["payload"]
    assert exception["subtype"] == "chase_requester_missing"
    assert set(("facts", "risk", "recommendation", "resolution_schema")) <= set(
        exception
    )


def test_known_send_refusal_waits_for_explicit_resolution(tmp_path, monkeypatch):
    env = P15._build(tmp_path, "t03-send-refused", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, _command = _activity_context(env, claim_id)
    monkeypatch.setattr(
        env.app.state.agent_runtime.comms,
        "send",
        lambda **_kwargs: {
            "status": "refused",
            "code": "TEMPLATE_NOT_REGISTERED",
            "review_id": None,
        },
    )
    result = activities._governed_send(  # noqa: SLF001 - negative seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=f"chase:{checklist_id.lower()}:0",
            step_id="chase_initial_request",
        )
    )
    assert result.status == "awaiting_review"
    assert P15._checklists(env.app, claim_id)[0]["status"] == "open"
    exception = P15._events(env.app, "review.created", claim_id)[-1]["payload"]
    assert exception["subtype"] == "chase_send_refused"
    assert exception["facts"]["outcome"] == "refused"


@pytest.mark.parametrize("inside_window", [True, False])
def test_inbound_deferral_window_has_exact_24_hour_boundary(
    tmp_path,
    inside_window,
):
    env = P15._build(
        tmp_path,
        f"t03-inbound-boundary-{inside_window}",
        model=P15._intimation_model(),
    )
    claim_id = P15._to_checklist(env)
    checklist_id, _run, activities, command = _initialise(env, claim_id)
    env.clock.advance_to(P15.T0 + timedelta(days=4))
    inbound_at = env.clock.now - timedelta(hours=24)
    if not inside_window:
        inbound_at -= timedelta(microseconds=1)
    communication, _created = env.app.state.claim_service.record_inbound_communication(
        graph_message_id=f"boundary-{inside_window}",
        claim_id=claim_id,
        thread_id="conv-intake-1",
        from_addr=P15.BROKER_ADDR,
        to_addrs=[P15.SELF_ADDRESS],
        subject="Boundary",
        body_text="Boundary",
    )
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE communications SET occurred_at = :inbound_at "
                "WHERE id = :communication_id"
            ),
            {
                "inbound_at": inbound_at,
                "communication_id": communication.id,
            },
        )
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :now "
                "WHERE checklist_id = :checklist_id"
            ),
            {"now": env.clock.now, "checklist_id": checklist_id},
        )
    state = activities._load(command)  # noqa: SLF001 - boundary seam
    assert state.step_id == ("chase_wait" if inside_window else "chase_reminder")


def test_uncertain_write_creates_four_part_exception(tmp_path):
    env = P15._build(tmp_path, "t03-uncertain", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, _command = _activity_context(env, claim_id)
    write_id = f"chase:{checklist_id.lower()}:0"
    result = activities._create_exception(  # noqa: SLF001 - Activity engine unit seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
            write_id=write_id,
            step_id="chase_initial_request",
        )
    )
    assert result.status == "awaiting_review"
    event = P15._events(env.app, "review.created", claim_id)[-1]["payload"]
    assert event["subtype"] == "uncertain_write"
    assert event["write_id"] == write_id
    assert set(("facts", "risk", "recommendation", "resolution_schema")) <= set(event)


@pytest.mark.parametrize(
    "subtype",
    [
        "chase_requester_missing",
        "chase_send_refused",
        "uncertain_write",
    ],
)
def test_rejected_recoverable_exception_keeps_collection_open(
    tmp_path,
    subtype,
):
    env = P15._build(
        tmp_path,
        f"t03-rejected-{subtype}",
        model=P15._intimation_model(),
    )
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, command = _initialise(env, claim_id)
    identity = {"checklist_id": checklist_id}
    if subtype == "uncertain_write":
        identity["write_id"] = f"chase:{checklist_id.lower()}:1"
    env.app.state.chase_agent.checklist.exception_once(
        claim_id=claim_id,
        subtype=subtype,
        identity=identity,
        payload={
            "facts": {"reason": "synthetic rejection boundary"},
            "risk": "automatic retry is unsafe",
            "recommendation": "reject to refuse automatic retry",
            "resolution_schema": "EXCEPTION@1",
            "role": "claims_officer",
        },
    )
    P15._drain(env.app)
    review = P15._items(
        env.app,
        claim_id=claim_id,
        type="EXCEPTION",
        subtype=subtype,
    )[0]
    response = P15._resolve(
        env,
        review["id"],
        P15.OFFICER_A,
        action="reject",
        schema_version="EXCEPTION@1",
        payload={
            "capability_id": "chase.checklist",
            "diff": P15._diff(),
            "reason": "do not retry automatically",
        },
    )
    assert response.status_code == 200
    P15._drain(env.app)

    state = activities._load(command)  # noqa: SLF001 - resolution boundary
    assert state.status == "running"
    assert state.step_id == "chase_wait"
    assert state.wake_at_epoch_ms is None
    terminal = activities._record_terminal(  # noqa: SLF001 - terminal guard
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
        )
    )
    assert terminal.status == "blocked"
    assert P15._checklists(env.app, claim_id)[0]["status"] == "open"
    assert P15._events(env.app, "chase.cancelled", claim_id) == []


def test_chase_exhausted_is_deduplicated_across_later_exception_history(tmp_path):
    env = P15._build(
        tmp_path,
        "t03-exhausted-dedup",
        model=P15._intimation_model(),
    )
    claim_id = P15._to_checklist(env)
    checklist_id, run, activities, command = _initialise(env, claim_id)
    env.clock.advance_to(P15.T0 + timedelta(days=40))
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :now, "
                "reminder_count = 6 WHERE checklist_id = :checklist_id"
            ),
            {"now": env.clock.now, "checklist_id": checklist_id},
        )

    first = activities._create_exception(command)  # noqa: SLF001 - Activity seam
    assert first.status == "awaiting_review"
    P15._drain(env.app)
    exhausted = P15._items(
        env.app,
        claim_id=claim_id,
        type="EXCEPTION",
        subtype="chase_exhausted",
    )
    assert len(exhausted) == 1
    approved = P15._resolve(
        env,
        exhausted[0]["id"],
        P15.OFFICER_A,
        action="approve",
        schema_version="EXCEPTION@1",
        payload={
            "capability_id": "chase.checklist",
            "diff": P15._diff(),
        },
    )
    assert approved.status_code == 200
    P15._drain(env.app)

    env.app.state.chase_agent.checklist.exception_once(
        claim_id=claim_id,
        subtype="chase_send_refused",
        identity={
            "checklist_id": checklist_id,
            "write_id": f"chase:{checklist_id.lower()}:6",
        },
        payload={
            "facts": {"outcome": "refused"},
            "risk": "the document request remains outstanding",
            "recommendation": "confirm whether another attempt is authorised",
            "resolution_schema": "EXCEPTION@1",
            "role": "claims_officer",
        },
    )
    P15._drain(env.app)
    refused = P15._items(
        env.app,
        claim_id=claim_id,
        type="EXCEPTION",
        subtype="chase_send_refused",
    )[0]
    resumed = P15._resolve(
        env,
        refused["id"],
        P15.OFFICER_A,
        action="approve",
        schema_version="EXCEPTION@1",
        payload={
            "capability_id": "chase.checklist",
            "diff": P15._diff(),
        },
    )
    assert resumed.status_code == 200
    P15._drain(env.app)

    state = activities._load(command)  # noqa: SLF001 - history lookup seam
    assert state.status == "running"
    assert state.step_id == "chase_wait"
    defensive = activities._create_exception(  # noqa: SLF001 - dedupe seam
        ControlCommand(
            run_ref=run["id"],
            claim_ref=claim_id,
            checklist_ref=checklist_id,
        )
    )
    assert defensive.status == "running"
    assert defensive.review_event_ref == first.review_event_ref
    exhausted_events = [
        event
        for event in P15._events(env.app, "review.created", claim_id)
        if event["payload"].get("subtype") == "chase_exhausted"
        and event["payload"].get("checklist_id") == checklist_id
    ]
    assert len(exhausted_events) == 1


def test_t03_worker_registries_are_explicit_and_pinned(tmp_path):
    env = P15._build(tmp_path, "t03-registration", model=P15._intimation_model())
    activities = env.app.state.chase_agent.temporal_activities(
        worker_build_id="b" * 40
    )
    assert {
        function.__temporal_activity_definition.name
        for function in chase_activity_registrations(activities)
    } == {
        "record_chase_started",
        "load_chase_state",
        "apply_chase_event",
        "governed_chase_send",
        "create_chase_exception",
        "record_chase_terminal",
    }
    assert CHASE_WORKFLOWS == (DocumentChaseWorkflow,)
    definition = DocumentChaseWorkflow.__temporal_workflow_definition
    assert definition.name == "DocumentChaseWorkflow"
    assert definition.versioning_behavior is VersioningBehavior.PINNED
    assert set(definition.signals) == {
        "pacha_event",
        "review_resolved",
        "claim_terminal",
        "document_received",
        "snooze_changed",
        "inbound_received",
    }
    assert set(definition.queries) == {"state"}


def test_t03_mapping_has_one_start_and_only_opaque_signals():
    starts = [mapping for mapping in TEMPORAL_INTENT_MAPPINGS if mapping.action == "start"]
    signals = [
        mapping for mapping in TEMPORAL_INTENT_MAPPINGS if mapping.action == "signal"
    ]
    assert [mapping.event_type for mapping in starts] == ["chase.workflow_requested"]
    assert all(mapping.signal_name is not None for mapping in signals)
    assert len({mapping.event_type for mapping in TEMPORAL_INTENT_MAPPINGS}) == len(
        TEMPORAL_INTENT_MAPPINGS
    )
    assert {
        event_type: ACTION_MAP[event_type]
        for event_type in (
            "chase.workflow_requested",
            "chase.inbound_received",
            "chase.review_resolved",
        )
    } == {
        "chase.workflow_requested": "chase.workflow_requested",
        "chase.inbound_received": "chase.inbound_received",
        "chase.review_resolved": "chase.review_resolved",
    }
