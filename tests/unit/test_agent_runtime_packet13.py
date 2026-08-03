"""Focused unit coverage for PACKET-13 recovery and blocked production seams."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent_runtime import build_agent_runtime
from claim_core import create_app, new_ulid
from cop_runtime import build_cop_runtime
from doc_intel.llm import FakeModelClient
from eval_harness import build_eval_harness
from intake_agent import build_intake_agent
from intake_agent.workflows import IntakeWorkflow
from orchestration.contracts import ControlCommand, ControlResult, ControlSignal
from orchestration.ids import intake_workflow_ref
from orchestration.policies import load_retry_policies
from review_queue import build_review_queue

MOTOR_PACK = Path(__file__).resolve().parents[2] / "packs" / "motor"
OFFICER = "user:01HINTAKEOFFICERA00000AAAA"
AGENT = "agent:intake"
STEP_IDS = (
    "create_claim",
    "ingest",
    "populate",
    "dupe_check",
    "late_check",
    "acknowledge",
    "checklist",
    "triage",
)


def _build(tmp_path, name: str, *, clock=None, model_client=None):
    app = create_app(f"sqlite:///{tmp_path}/{name}.db", clock=clock)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app, model_client=model_client)
    build_review_queue(app, roles={OFFICER: "claims_officer"})
    runtime = build_agent_runtime(app)
    return app, runtime


class TemporalIntakeFixture:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ingest_attempts = 0
        self.populate_attempts = 0

    def _result(self, step_id: str, status: str = "running") -> ControlResult:
        self.calls.append(step_id)
        return ControlResult(status=status, run_ref="01H00000000000000000000001")

    @activity.defn(name="intake_create_claim")
    async def create_claim(self, _command: ControlCommand) -> ControlResult:
        return self._result("create_claim")

    @activity.defn(name="intake_ingest")
    async def ingest(self, _command: ControlCommand) -> ControlResult:
        self.ingest_attempts += 1
        if self.ingest_attempts < 3:
            raise RuntimeError("retryable database fixture")
        return self._result("ingest")

    @activity.defn(name="intake_populate")
    async def populate(self, _command: ControlCommand) -> ControlResult:
        self.populate_attempts += 1
        return self._result(
            "populate", "awaiting_review" if self.populate_attempts == 1 else "running"
        )

    @activity.defn(name="intake_dupe_check")
    async def dupe_check(self, _command: ControlCommand) -> ControlResult:
        return self._result("dupe_check")

    @activity.defn(name="intake_late_check")
    async def late_check(self, _command: ControlCommand) -> ControlResult:
        return self._result("late_check")

    @activity.defn(name="intake_acknowledge")
    async def acknowledge(self, _command: ControlCommand) -> ControlResult:
        return self._result("acknowledge")

    @activity.defn(name="intake_checklist")
    async def checklist(self, _command: ControlCommand) -> ControlResult:
        return self._result("checklist")

    @activity.defn(name="intake_triage")
    async def triage(self, _command: ControlCommand) -> ControlResult:
        return self._result("triage", "completed")


async def _run_temporal_resume_and_retry() -> TemporalIntakeFixture:
    load_retry_policies()
    fixture = TemporalIntakeFixture()
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with (
            Worker(
                environment.client,
                task_queue="pacha-test-control-v1",
                workflows=[IntakeWorkflow],
                activities=[
                    fixture.create_claim,
                    fixture.ingest,
                    fixture.populate,
                    fixture.dupe_check,
                    fixture.late_check,
                    fixture.checklist,
                    fixture.triage,
                ],
            ),
            Worker(
                environment.client,
                task_queue="pacha-test-effects-v1",
                activities=[fixture.acknowledge],
            ),
        ):
            trigger_ref = "01H00000000000000000000002"
            handle = await environment.client.start_workflow(
                IntakeWorkflow.run,
                ControlCommand(
                    run_ref="01H00000000000000000000001",
                    trigger_event_ref=trigger_ref,
                ),
                id=str(intake_workflow_ref(trigger_ref)),
                task_queue="pacha-test-control-v1",
            )
            for _ in range(500):
                if fixture.populate_attempts == 1:
                    break
                await asyncio.sleep(0.01)
            state = await handle.query(IntakeWorkflow.state)
            assert state.status == "awaiting_review"
            await handle.signal(
                IntakeWorkflow.review_resolved,
                ControlSignal(event_ref="01H00000000000000000000003"),
            )
            result = await handle.result()
            assert result.status == "completed"
    return fixture


def test_temporal_retries_resumes_after_review_and_completes(tmp_path):
    fixture = asyncio.run(_run_temporal_resume_and_retry())
    assert fixture.ingest_attempts == 3
    assert fixture.populate_attempts == 2
    assert fixture.calls == [
        "create_claim",
        "ingest",
        "populate",
        "populate",
        "dupe_check",
        "late_check",
        "acknowledge",
        "checklist",
        "triage",
    ]
    _app, runtime = _build(tmp_path, "temporal-recovery")
    assert not hasattr(runtime, "start_run")
    assert not hasattr(runtime, "run")
    assert not hasattr(runtime, "reap")
    assert not hasattr(runtime.runner, "reap")


def test_pending_transport_refuses_without_graph_registration(tmp_path):
    app, runtime = _build(tmp_path, "blocked")
    client = TestClient(app)
    claim_id = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers={"X-Actor": AGENT},
    ).json()["id"]
    party_id = new_ulid()
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO parties (id, claim_id, role, name, email) "
                "VALUES (:id, :claim, 'insured', 'Amina', 'amina@example.co.ke')"
            ),
            {"id": party_id, "claim": claim_id},
        )
        connection.execute(
            text(
                "UPDATE capabilities SET current_level = 'L4' "
                "WHERE id = 'intake.acknowledge'"
            )
        )
    outcome = runtime.comms.send(
        template_id="T-06a",
        claim_id=claim_id,
        to_party_ids=[party_id],
        attachments=(),
        capability_id="intake.acknowledge",
        actor=AGENT,
    )
    assert outcome == {"status": "refused", "code": None, "review_id": None}


def test_production_classifier_uses_structured_wrapper_and_logs(tmp_path):
    model = FakeModelClient(
        [
            {
                "data": {"class": "new_intimation", "confidence": 0.91},
                "cost_usd": 0.001,
                "model_id": "claude-haiku-fixture",
            }
        ]
    )
    app, _runtime = _build(tmp_path, "classifier", model_client=model)
    agent = build_intake_agent(
        app,
        officers=[OFFICER],
        config={"self_addresses": [], "archive_sample_rate": 10},
    )
    result = agent.router.classifier.classify(
        {
            "graph_message_id": "classifier-message",
            "conversation_id": None,
            "from_addr": "amina@example.co.ke",
            "to_addrs": ["claims@mayfair.co.ke"],
            "subject": "Motor claim",
            "body_text": "Please register my loss",
            "attachments": [],
        }
    )
    assert result == {"class": "new_intimation", "confidence": 0.91}
    assert model.calls[0]["tier"] == "MODEL_LIGHT"
    with app.state.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'model.called'")
        ).scalar_one() == 1


def test_identical_attachment_is_not_duplicated_or_dropped(tmp_path):
    app, _runtime = _build(tmp_path, "duplicate_attachment")
    build_intake_agent(
        app,
        classifier=SimpleNamespace(
            classify=lambda _message: {"class": "unclear", "confidence": 1}
        ),
        officers=[OFFICER],
        config={"self_addresses": [], "archive_sample_rate": 0},
    )
    client = TestClient(app)
    claim_id = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers={"X-Actor": AGENT},
    ).json()["id"]
    app.state.claim_service.record_inbound_communication(
        graph_message_id="seed-thread",
        claim_id=claim_id,
        thread_id="duplicate-thread",
        from_addr="amina@example.co.ke",
        to_addrs=["claims@mayfair.co.ke"],
        subject="Claim",
        body_text="seed",
    )
    encoded = base64.b64encode(b"same-attachment-bytes").decode("ascii")
    for message_id in ("duplicate-1", "duplicate-2"):
        with Session(app.state.engine) as session:
            app.state.record_event(
                session,
                claim_id=None,
                event_type="email.received",
                payload={
                    "graph_message_id": message_id,
                    "conversation_id": "duplicate-thread",
                    "from_addr": "amina@example.co.ke",
                    "to_addrs": ["claims@mayfair.co.ke"],
                    "subject": "Re: Claim",
                    "body_text": "same file attached again",
                    "attachments": [
                        {
                            "filename": "photo.png",
                            "mime": "image/png",
                            "content_b64": encoded,
                        }
                    ],
                },
                actor=AGENT,
                correlation_id=None,
            )
            session.commit()
    for _ in range(24):
        if app.state.dispatcher.dispatch_once() == 0:
            break
    with app.state.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM documents WHERE claim_id = :claim_id"),
            {"claim_id": claim_id},
        ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM communications WHERE graph_message_id "
                "IN ('seed-thread','duplicate-1','duplicate-2')"
            )
        ).scalar_one() == 3
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM events "
                "WHERE type = 'INBOUND_DUPLICATE_ATTACHMENT' AND claim_id = :claim_id"
            ),
            {"claim_id": claim_id},
        ).scalar_one() == 1
