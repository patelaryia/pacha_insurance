"""Focused unit coverage for PACKET-13 recovery and blocked production seams."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from agent_runtime import build_agent_runtime
from claim_core import create_app, new_ulid
from cop_runtime import build_cop_runtime
from doc_intel.llm import FakeModelClient
from eval_harness import build_eval_harness
from intake_agent import build_intake_agent
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


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, minutes: int) -> None:
        self.value += timedelta(minutes=minutes)


def _build(tmp_path, name: str, *, clock=None, model_client=None):
    app = create_app(f"sqlite:///{tmp_path}/{name}.db", clock=clock)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app, model_client=model_client)
    build_review_queue(app, roles={OFFICER: "claims_officer"})
    runtime = build_agent_runtime(app)
    return app, runtime


def test_runner_resumes_after_review_and_completes(tmp_path):
    app, runtime = _build(tmp_path, "resume")
    calls: list[str] = []

    def step(context):
        calls.append(context.step_id)
        if context.step_id == "populate":
            return {"status": "staged", "review_id": "review-fixture"}
        return {"status": "ok"}

    for step_id in STEP_IDS:
        runtime.register_step("intake.claim_creation", step_id, step)
    run_id = runtime.start_run(agent="intake", capability_id="intake.claim_creation")
    awaiting = runtime.run(run_id)
    assert awaiting["status"] == "awaiting_review"
    assert calls == ["create_claim", "ingest", "populate"]

    runtime.runner.consume(
        SimpleNamespace(
            type="review.resolved",
            payload={"agent_run_id": run_id},
        )
    )
    with app.state.engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, steps, ended_at FROM agent_runs WHERE id = :id"),
            {"id": run_id},
        ).mappings().one()
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    steps = json.loads(row["steps"]) if isinstance(row["steps"], str) else row["steps"]
    assert all(step_row["status"] == "completed" for step_row in steps)
    assert calls == list(STEP_IDS)


def test_runner_heartbeat_reaper_and_exhausted_failure(tmp_path):
    clock = MutableClock()
    app, runtime = _build(tmp_path, "reaper", clock=clock)

    def failing(_context):
        raise RuntimeError("injected step failure")

    runtime.register_step("intake.claim_creation", "create_claim", failing)
    run_id = runtime.start_run(agent="intake", capability_id="intake.claim_creation")
    first = runtime.run(run_id)
    assert first["status"] == "running"
    runtime.runner.heartbeat(run_id, "create_claim")

    for _ in range(2):
        clock.advance(minutes=16)
        assert runtime.reap() == 1
    clock.advance(minutes=16)
    assert runtime.reap() == 1
    with app.state.engine.connect() as connection:
        run = connection.execute(
            text("SELECT status, error FROM agent_runs WHERE id = :id"),
            {"id": run_id},
        ).mappings().one()
        failures = connection.execute(
            text(
                "SELECT COUNT(*) FROM events WHERE type = 'review.created' "
                "AND correlation_id = :id"
            ),
            {"id": run_id},
        ).scalar_one()
    assert run["status"] == "failed"
    error = json.loads(run["error"]) if isinstance(run["error"], str) else run["error"]
    assert error["code"] == "STEP_ATTEMPTS_EXHAUSTED"
    assert failures == 1


def test_missing_step_blocks_and_pending_transport_refuses(tmp_path):
    app, runtime = _build(tmp_path, "blocked")
    run_id = runtime.start_run(agent="intake", capability_id="intake.claim_creation")
    assert runtime.run(run_id)["status"] == "blocked"

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
