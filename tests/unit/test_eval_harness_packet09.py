"""Focused PACKET-09 fail-closed and orchestration unit coverage."""

from __future__ import annotations

import io
import json
import pathlib
from collections import defaultdict
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, text

from claim_core.app import create_app
from claim_core.models import Event
from claim_core.service import new_ulid
from cop_runtime import build_cop_runtime
from eval_harness import PromotionDenied, build_eval_harness
from eval_harness.anonymise import AnonymisationRefused, anonymise_bundle, main
from eval_harness.graders import CitationGrader
from eval_harness.models import Capability, GraderRun

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"
AGENT = {"X-Actor": "agent:eval"}
HUMAN = {"X-Actor": "user:01ARZ3NDEKTSV4RRFFQ69G5FAV"}


class QueuedModel:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = defaultdict(list)
        self.calls: list[dict] = []

    def queue_raw(self, task: str, response: dict) -> None:
        self.responses[task].append(response)

    def queue_data(self, task: str, data: dict) -> None:
        self.queue_raw(
            task,
            {"data": data, "cost_usd": 0.0, "model_id": "unit-model"},
        )

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        del schema
        self.calls.append({"tier": tier, "inputs": dict(inputs)})
        return self.responses[inputs["task"]].pop(0)


class FailingExecutor:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def execute(self, case):
        del case
        if self.empty:
            return []
        raise RuntimeError("fixture replay failure")


@pytest.fixture()
def eval_app(tmp_path):
    model = QueuedModel()
    app = create_app(f"sqlite:///{tmp_path}/packet09-unit.db")
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    harness = build_eval_harness(app, model_client=model)
    return TestClient(app), app, harness, model


def _png(label: str) -> bytes:
    image = Image.new("RGB", (200, 100), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue() + label.encode()


def _claim(client: TestClient) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=AGENT,
    )
    assert response.status_code == 201
    return response.json()["id"]


def _citation(client: TestClient, app, *, corrupt: bool = False) -> str:
    claim_id = _claim(client)
    response = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": ("source.png", io.BytesIO(_png("source")), "image/png")},
        data={"source_channel": "test", "source_ref": "unit"},
        headers=AGENT,
    )
    document_id = response.json()["id"]
    app.state.blob_store.put(
        f"pages/{document_id}/1.png",
        b"not-a-png" if corrupt else _png("KDA 123B"),
    )
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": "vehicle.reg",
                    "value": "KDA 123B",
                    "value_type": "string",
                    "source_type": "extraction",
                    "source_ref": {
                        "document_id": document_id,
                        "page": 1,
                        "bbox": [0.0, 0.0, 1.0, 1.0],
                        "anchor_text": "KDA 123B",
                    },
                    "confidence": 0.99,
                    "verification_state": "extracted",
                }
            ]
        },
        headers=AGENT,
    )
    assert response.status_code == 200
    return claim_id


def _note(client: TestClient, app) -> tuple[str, str]:
    claim_id = _claim(client)
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": "reserve.total",
                    "value": 250_000_00,
                    "value_type": "money",
                    "source_type": "human",
                    "verification_state": "human_verified",
                }
            ]
        },
        headers=HUMAN,
    )
    assert response.status_code == 200
    key = f"artifacts/{claim_id}/T-01.txt"
    app.state.blob_store.put(key, b"Recommendation\nReserve KES 250,000.\nVerification")
    return claim_id, key


def _grade_note(harness, claim_id: str, key: str):
    return harness.grade(
        "G-NOTE",
        {
            "claim_id": claim_id,
            "blob_key": key,
            "template_id": "T-01",
            "capability_id": "pack.note_draft",
        },
        "agent:eval",
    )


def _note_response(**overrides):
    response = {
        "numeric_claims": [
            {
                "text": "KES 250,000",
                "field_path": "reserve.total",
                "observed_value": "KES 250,000",
                "value_type": "money",
            }
        ],
        "unsupported_assertions": [],
        "missing_sections": [],
        "tone_ok": True,
    }
    response.update(overrides)
    return response


def test_gcite_corrupt_render_and_schema_failure_are_errors(eval_app):
    client, app, harness, model = eval_app
    corrupt_claim = _citation(client, app, corrupt=True)
    result = harness.grade(
        "G-CITE",
        {"claim_id": corrupt_claim, "path": "vehicle.reg"},
        "agent:eval",
    )
    assert result.result == "error"
    assert model.calls == []

    claim_id = _citation(client, app)
    invalid = {"data": {}, "cost_usd": 0.0, "model_id": "unit-model"}
    model.queue_raw("g_cite_verify", invalid)
    model.queue_raw("g_cite_verify", invalid)
    result = harness.grade(
        "G-CITE",
        {"claim_id": claim_id, "path": "vehicle.reg"},
        "agent:eval",
    )
    assert result.result == "error"
    assert result.detail["error_type"] == "ModelSchemaError"


def test_model_graders_reject_invalid_subjects_fields_and_model_answers(eval_app):
    client, app, harness, model = eval_app
    assert harness.grade("G-CITE", {}, "agent:eval").result == "error"
    assert (
        harness.grade(
            "G-CITE",
            {"claim_id": "missing", "path": "vehicle.reg"},
            "agent:eval",
        ).result
        == "error"
    )
    assert harness.grade("G-NOTE", {}, "agent:eval").result == "error"

    claim_id, key = _note(client, app)
    app.state.blob_store.put(key, b"\xff\xfe")
    assert _grade_note(harness, claim_id, key).result == "error"

    app.state.blob_store.put(key, b"Recommendation\nVerification")
    invalid = {"data": {}, "cost_usd": 0.0, "model_id": "unit-model"}
    model.queue_raw("g_note_grade", invalid)
    model.queue_raw("g_note_grade", invalid)
    assert _grade_note(harness, claim_id, key).result == "error"

    cited_claim = _citation(client, app)
    model.queue_data(
        "g_cite_verify",
        {"value_present": False, "observed_value": None},
    )
    assert (
        harness.grade(
            "G-CITE",
            {"claim_id": cited_claim, "path": "vehicle.reg"},
            "agent:eval",
        ).result
        == "fail"
    )
    assert not CitationGrader._exact_numeric("date", None, "2026-07-12")
    assert not CitationGrader._exact_numeric("date", "bad-date", "2026-07-12")
    assert CitationGrader._exact_numeric("string", "1", "2")


@pytest.mark.parametrize(
    "response",
    [
        _note_response(tone_ok=False),
        _note_response(missing_sections=["verification"]),
        _note_response(
            numeric_claims=[
                {
                    "text": "KES 250,000",
                    "field_path": "unknown.money",
                    "observed_value": "KES 250,000",
                    "value_type": "money",
                }
            ]
        ),
        _note_response(
            numeric_claims=[
                {
                    "text": "KES 250,000",
                    "field_path": "reserve.total",
                    "observed_value": "not-money",
                    "value_type": "money",
                }
            ]
        ),
    ],
)
def test_gnote_rubric_unknown_field_and_parse_fail_closed(eval_app, response):
    client, app, harness, model = eval_app
    claim_id, key = _note(client, app)
    model.queue_data("g_note_grade", response)
    assert _grade_note(harness, claim_id, key).result == "fail"


@pytest.mark.parametrize("empty", [False, True])
def test_corpus_executor_failures_are_counted_not_passed(eval_app, empty):
    _client, _app, harness, _model = eval_app
    harness.corpus.executor = FailingExecutor(empty=empty)
    harness.corpus.create_case(
        corpus="motor_v1",
        origin="seed_closed_claim",
        input_bundle={"fixture": "executor-error"},
        expected={"fields": {}},
        tags=["capability:intake.claim_creation"],
    )
    result = harness.corpus.run(
        corpus="motor_v1",
        capability_id=None,
        actor="agent:eval",
    )
    card = result.scorecards["intake.claim_creation"]
    assert card["errors"] == 1
    assert card["passed"] == 0
    assert card["pass_percent"] == 0


def test_named_weekly_task_uses_sync_engine_and_requires_runtime(eval_app, monkeypatch):
    from eval_harness import tasks

    _client, _app, _harness, _model = eval_app
    result = tasks.run_weekly_corpus.run()
    assert result["corpus"] == "motor_v1"
    monkeypatch.setattr(tasks, "_HARNESS", None)
    with pytest.raises(RuntimeError):
        tasks.run_weekly_corpus.run()


def test_corpus_grades_are_excluded_from_autonomy_evidence(eval_app):
    _client, _app, harness, _model = eval_app
    capability_id = "intake.acknowledge"
    now = harness.clock()
    with harness.sessions.begin() as session:
        session.add_all(
            [
                GraderRun(
                    id=new_ulid(),
                    grader_id="G-CITE",
                    subject_type="field",
                    subject_ref={"capability_id": capability_id},
                    claim_id=None,
                    test_case_id=None,
                    result="fail",
                    severity="critical",
                    detail={},
                    occurred_at=now,
                ),
                GraderRun(
                    id=new_ulid(),
                    grader_id="G-CITE",
                    subject_type="field",
                    subject_ref={"capability_id": capability_id},
                    claim_id=None,
                    test_case_id="synthetic-case",
                    result="pass",
                    severity="critical",
                    detail={},
                    occurred_at=now,
                ),
            ]
        )
    evidence = harness.autonomy.evidence(capability_id)
    assert evidence["grader_window_count"] == 1
    assert evidence["grader_pass_percent"] == 0
    assert evidence["critical_failures"] == 1


def test_synthetic_passes_cannot_satisfy_production_promotion_window(eval_app):
    _client, app, harness, _model = eval_app
    capability_id = "intake.acknowledge"
    now = harness.clock()
    with harness.sessions.begin() as session:
        for _ in range(25):
            app.state.record_event(
                session,
                claim_id=None,
                event_type="review.resolved",
                payload={
                    "capability_id": capability_id,
                    "resolution": "approved",
                    "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
                },
                actor=HUMAN["X-Actor"],
                correlation_id=None,
            )
        session.add(
            GraderRun(
                id=new_ulid(),
                grader_id="G-CITE",
                subject_type="field",
                subject_ref={"capability_id": capability_id},
                claim_id=None,
                test_case_id=None,
                result="fail",
                severity="critical",
                detail={},
                occurred_at=now,
            )
        )
        for index in range(24):
            session.add(
                GraderRun(
                    id=new_ulid(),
                    grader_id="G-CITE",
                    subject_type="field",
                    subject_ref={"capability_id": capability_id},
                    claim_id=None,
                    test_case_id=f"synthetic-{index}",
                    result="pass",
                    severity="critical",
                    detail={},
                    occurred_at=now + timedelta(seconds=1),
                )
            )
    with pytest.raises(PromotionDenied) as error:
        harness.autonomy.request_promotion(
            capability_id,
            "L2",
            sign_offs=[{"actor": HUMAN["X-Actor"], "role": "claims_manager"}],
            actor=HUMAN["X-Actor"],
        )
    assert error.value.code == "CRITERIA_NOT_MET"


def test_corpus_grader_failure_event_cannot_demote_live_capability(eval_app):
    _client, app, harness, _model = eval_app
    capability_id = "intake.acknowledge"
    with harness.sessions.begin() as session:
        session.get(Capability, capability_id).current_level = "L3"
    harness.grade(
        "G-CITE",
        {
            "capability_id": capability_id,
            "test_case_id": "synthetic-case",
        },
        "agent:eval",
    )
    app.state.dispatcher.dispatch_once(consumers=["autonomy"])
    assert harness.autonomy.level(capability_id) == "L3"
    with harness.engine.connect() as connection:
        alerts = connection.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'ops.alert'")
        ).scalar_one()
    assert alerts == 0


def test_unknown_correction_capture_is_blocked_and_idempotent(eval_app):
    client, app, harness, _model = eval_app
    claim_id = _claim(client)
    with harness.sessions.begin() as session:
        event = app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="review.resolved",
            payload={"capability_id": "pack.note_draft", "resolution": "mystery"},
            actor=HUMAN["X-Actor"],
            correlation_id=None,
        )
        session.flush()
        event_id = event.id
    with harness.sessions() as session:
        event = session.scalar(select(Event).where(Event.id == event_id))
        session.expunge(event)
    harness.corpus.consume(event)
    harness.corpus.consume(event)
    with harness.engine.connect() as connection:
        rows = list(connection.execute(text("SELECT expected FROM test_cases")))
    assert len(rows) == 1
    expected = rows[0][0]
    if isinstance(expected, str):
        expected = json.loads(expected)
    assert expected["_capture"]["status"] == "blocked_on_inputs"


def test_correction_with_changed_prose_requires_immutable_reference(eval_app):
    client, app, harness, _model = eval_app
    claim_id = _claim(client)
    with harness.sessions.begin() as session:
        event = app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="review.resolved",
            payload={
                "capability_id": "pack.note_draft",
                "resolution": "edited",
                "diff": {"typed_changes": [], "prose_change_ratio": 0.5},
            },
            actor=HUMAN["X-Actor"],
            correlation_id=None,
        )
        session.flush()
        event_id = event.id
    with harness.sessions() as session:
        event = session.scalar(select(Event).where(Event.id == event_id))
        session.expunge(event)
    harness.corpus.consume(event)
    with harness.engine.connect() as connection:
        expected = connection.execute(text("SELECT expected FROM test_cases")).scalar_one()
    if isinstance(expected, str):
        expected = json.loads(expected)
    assert "corrected_prose_ref" in expected["_capture"]["missing_inputs"]


@pytest.mark.parametrize(
    "bundle",
    [
        {"fields": [{"path": "reserve.total", "value": True, "value_type": "money"}]},
        {
            "fields": [
                {
                    "path": "parties.insured.name",
                    "value": "Wanjiku",
                    "value_type": "string",
                }
            ]
        },
        {"fields": [], "email": {"body": "Call Wanjiku on 0712345678"}},
        {"fields": [], "metadata": {"notes": "Call Wanjiku on 0712345678"}},
        {"fields": [], "metadata": {"description": "Wanjiku's claim"}},
        {"fields": [], "attachment": {"mime": "image/png", "value": "base64"}},
        {
            "fields": [
                {
                    "path": "claim.metadata",
                    "value": {"name": "Wanjiku Kamau", "phone": "+254712345678"},
                    "value_type": "object",
                    "pii_class": "none",
                }
            ]
        },
    ],
)
def test_anonymiser_refuses_money_bool_unclassified_pii_text_and_images(bundle):
    with pytest.raises(AnonymisationRefused):
        anonymise_bundle(bundle, claim_key="claim-a", secret=b"secret")


def test_anonymiser_cli_requires_secret_and_refuses_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    target = tmp_path / "anonymised.json"
    source.write_text(
        json.dumps(
            {
                "fields": [
                    {
                        "path": "parties.insured.name",
                        "value": "Wanjiku Kamau",
                        "value_type": "string",
                        "pii_kind": "name",
                    }
                ]
            }
        )
    )
    monkeypatch.delenv("PACHA_ANONYMISATION_SECRET", raising=False)
    with pytest.raises(AnonymisationRefused):
        main([str(source), str(target), "--claim-key", "claim-a"])
    assert not target.exists()

    monkeypatch.setenv("PACHA_ANONYMISATION_SECRET", "runtime-secret")
    assert main([str(source), str(target), "--claim-key", "claim-a"]) == 0
    assert "Wanjiku Kamau" not in target.read_text()
    with pytest.raises(AnonymisationRefused):
        main([str(source), str(target), "--claim-key", "claim-a"])
