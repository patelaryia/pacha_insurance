"""PACKET-09 acceptance — PRD-03 §3.3/§3.5/§3.7 model graders + corpus.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-09_eval_corpus.md §3. The suite uses injected model and
corpus-executor fakes; no network, broker, or live model call is permitted.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
from collections import defaultdict

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

AGENT = {"X-Actor": "agent:eval"}
HUMAN = {"X-Actor": "user:01ARZ3NDEKTSV4RRFFQ69G5FAV"}
ACTOR = "agent:eval"

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"


class TaskModel:
    """Strict queued fake: every model grade declares a stable configured task."""

    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = defaultdict(list)
        self.calls: list[dict] = []

    def queue(self, task: str, data: dict) -> None:
        self.responses[task].append(data)

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        task = inputs["task"]
        self.calls.append({"tier": tier, "schema": schema, "inputs": dict(inputs)})
        if not self.responses[task]:
            raise AssertionError(f"unexpected model task {task!r}")
        return {
            "data": self.responses[task].pop(0),
            "cost_usd": 0.0,
            "model_id": f"fake-{tier.casefold()}",
        }


class FixtureCorpusExecutor:
    def __init__(self) -> None:
        self.observations: dict[str, list[object]] = {}
        self.calls: list[str] = []

    def execute(self, case) -> list[object]:
        self.calls.append(case.id)
        return list(self.observations.get(case.id, []))


@pytest.fixture()
def harness(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness

    model = TaskModel()
    executor = FixtureCorpusExecutor()
    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc9.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    evals = build_eval_harness(
        app,
        model_client=model,
        corpus_executor=executor,
    )
    return TestClient(app), app, evals, model, executor


def _claim(client) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=AGENT,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, claim_id: str, label: str = "source") -> str:
    response = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (f"{label}.png", io.BytesIO(_png(label)), "image/png")},
        data={"source_channel": "test", "source_ref": f"msg-{label}"},
        headers=AGENT,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _png(label: str) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(image).text((40, 100), label, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _extracted(
    client,
    claim_id: str,
    document_id: str,
    *,
    path: str,
    value,
    value_type: str,
    anchor: str,
) -> None:
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": value,
                    "value_type": value_type,
                    "source_type": "extraction",
                    "source_ref": {
                        "document_id": document_id,
                        "page": 1,
                        "bbox": [0.05, 0.1, 0.95, 0.9],
                        "anchor_text": anchor,
                    },
                    "confidence": 0.99,
                    "verification_state": "extracted",
                }
            ]
        },
        headers=AGENT,
    )
    assert response.status_code == 200, response.text


def _human_write(
    client, claim_id: str, *, path: str, value, value_type: str
) -> None:
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": path,
                    "value": value,
                    "value_type": value_type,
                    "source_type": "human",
                    "verification_state": "human_verified",
                }
            ]
        },
        headers=HUMAN,
    )
    assert response.status_code == 200, response.text


def _emit(app, event_type: str, payload: dict, claim_id: str | None = None) -> str:
    with Session(app.state.engine) as session:
        event = app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=HUMAN["X-Actor"],
            correlation_id=None,
        )
        session.commit()
        return event.id


def _drain(app, cycles: int = 16) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _cases(app) -> list[dict]:
    with app.state.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, corpus, origin, input_bundle, expected, tags "
                "FROM test_cases ORDER BY created_at, id"
            )
        ).mappings()
        output = []
        for row in rows:
            item = dict(row)
            for key in ("input_bundle", "expected", "tags"):
                if isinstance(item[key], str):
                    item[key] = json.loads(item[key])
            output.append(item)
        return output


def _resolved(
    app,
    claim_id: str,
    *,
    capability_id: str,
    resolution: str,
    typed_changes: list[dict] | None = None,
) -> str:
    return _emit(
        app,
        "review.resolved",
        {
            "capability_id": capability_id,
            "resolution": resolution,
            "diff": {
                "typed_changes": typed_changes or [],
                "prose_change_ratio": 0.0,
            },
        },
        claim_id,
    )


def _citation_field(harness, *, path: str, value, value_type: str, label: str):
    client, app, _, _, _ = harness
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, label)
    app.state.blob_store.put(f"pages/{document_id}/1.png", _png(label))
    _extracted(
        client,
        claim_id,
        document_id,
        path=path,
        value=value,
        value_type=value_type,
        anchor=label,
    )
    return claim_id, document_id


# --- model grader registry + G-CITE -----------------------------------------------


def test_model_graders_flip_live_only(harness):
    _, _, evals, _, _ = harness
    assert evals.graders.get("G-CITE").status == "live"
    assert evals.graders.get("G-NOTE").status == "live"
    assert evals.graders.get("G-COMM").status == "pending"
    assert evals.graders.get("G-PROC").status == "pending"


def test_gcite_crops_real_citation_and_uses_model_light(harness):
    _, _, evals, model, _ = harness
    claim_id, _ = _citation_field(
        harness,
        path="vehicle.reg",
        value="KDA 123B",
        value_type="string",
        label="KDA 123B",
    )
    model.queue("g_cite_verify", {"value_present": True, "observed_value": None})
    result = evals.grade(
        "G-CITE",
        {
            "claim_id": claim_id,
            "path": "vehicle.reg",
            "capability_id": "intake.claim_creation",
        },
        ACTOR,
    )
    assert result.result == "pass"
    call = model.calls[-1]
    assert call["tier"] == "MODEL_LIGHT"
    assert call["inputs"]["task"] == "g_cite_verify"
    assert call["inputs"]["crop_png"].startswith(b"\x89PNG")
    assert "page_png" not in call["inputs"]


def test_gcite_money_is_exact_after_kes_parse(harness):
    _, _, evals, model, _ = harness
    claim_id, _ = _citation_field(
        harness,
        path="assessment.estimate_total",
        value=75_000_00,
        value_type="money",
        label="KES 75,000",
    )
    model.queue(
        "g_cite_verify",
        {"value_present": True, "observed_value": "KES 75,000.01"},
    )
    failed = evals.grade(
        "G-CITE", {"claim_id": claim_id, "path": "assessment.estimate_total"}, ACTOR
    )
    assert failed.result == "fail"

    model.queue(
        "g_cite_verify",
        {"value_present": True, "observed_value": "KES 75,000"},
    )
    passed = evals.grade(
        "G-CITE", {"claim_id": claim_id, "path": "assessment.estimate_total"}, ACTOR
    )
    assert passed.result == "pass"


def test_gcite_date_is_exact_after_parse(harness):
    _, _, evals, model, _ = harness
    claim_id, _ = _citation_field(
        harness,
        path="loss.date",
        value="2026-07-12",
        value_type="date",
        label="12 July 2026",
    )
    model.queue(
        "g_cite_verify",
        {"value_present": True, "observed_value": "2026-07-13"},
    )
    failed = evals.grade("G-CITE", {"claim_id": claim_id, "path": "loss.date"}, ACTOR)
    assert failed.result == "fail"

    model.queue(
        "g_cite_verify",
        {"value_present": True, "observed_value": "2026-07-12"},
    )
    passed = evals.grade("G-CITE", {"claim_id": claim_id, "path": "loss.date"}, ACTOR)
    assert passed.result == "pass"


def test_gcite_missing_page_render_is_error_without_model_call(harness):
    client, _, evals, model, _ = harness
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, "render absent")
    _extracted(
        client,
        claim_id,
        document_id,
        path="vehicle.reg",
        value="KDA 123B",
        value_type="string",
        anchor="KDA 123B",
    )
    result = evals.grade(
        "G-CITE", {"claim_id": claim_id, "path": "vehicle.reg"}, ACTOR
    )
    assert result.result == "error"
    assert not model.calls


def test_extracted_field_event_runs_gcite_once_per_field_version(harness):
    _, app, _, model, _ = harness
    _citation_field(
        harness,
        path="vehicle.reg",
        value="KDA 123B",
        value_type="string",
        label="KDA 123B event",
    )
    model.queue("g_cite_verify", {"value_present": True, "observed_value": None})
    _drain(app)
    _drain(app)
    with app.state.engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM grader_runs WHERE grader_id = 'G-CITE'")
        ).scalar_one()
    assert count == 1


# --- G-NOTE -----------------------------------------------------------------------


def _note_subject(harness, prose: str) -> tuple[str, str]:
    client, app, _, _, _ = harness
    claim_id = _claim(client)
    _human_write(
        client,
        claim_id,
        path="reserve.total",
        value=250_000_00,
        value_type="money",
    )
    _human_write(
        client,
        claim_id,
        path="loss.date",
        value="2026-07-12",
        value_type="date",
    )
    blob_key = f"artifacts/{claim_id}/T-01.txt"
    app.state.blob_store.put(blob_key, prose.encode("utf-8"))
    return claim_id, blob_key


def _note_grade(evals, claim_id: str, blob_key: str):
    return evals.grade(
        "G-NOTE",
        {
            "claim_id": claim_id,
            "blob_key": blob_key,
            "template_id": "T-01",
            "capability_id": "pack.note_draft",
        },
        ACTOR,
    )


def _clean_note_response() -> dict:
    return {
        "numeric_claims": [
            {
                "text": "KES 250,000",
                "field_path": "reserve.total",
                "observed_value": "KES 250,000",
                "value_type": "money",
            },
            {
                "text": "12 July 2026",
                "field_path": "loss.date",
                "observed_value": "2026-07-12",
                "value_type": "date",
            },
        ],
        "unsupported_assertions": [],
        "missing_sections": [],
        "tone_ok": True,
    }


def test_gnote_passes_only_after_independent_structured_comparison(harness):
    _, _, evals, model, _ = harness
    claim_id, blob_key = _note_subject(
        harness,
        "Recommendation\nReserve KES 250,000. Loss date 12 July 2026.\nVerification",
    )
    model.queue("g_note_grade", _clean_note_response())
    result = _note_grade(evals, claim_id, blob_key)
    assert result.result == "pass"
    assert result.severity == "major"
    assert model.calls[-1]["tier"] == "MODEL_HEAVY"
    assert model.calls[-1]["inputs"]["task"] == "g_note_grade"


def test_gnote_fails_unsupported_assertion(harness):
    _, _, evals, model, _ = harness
    claim_id, blob_key = _note_subject(
        harness, "Recommendation\nReserve KES 250,000.\nVerification"
    )
    response = _clean_note_response()
    response["numeric_claims"] = response["numeric_claims"][:1]
    response["unsupported_assertions"] = ["The insured admitted liability"]
    model.queue("g_note_grade", response)
    assert _note_grade(evals, claim_id, blob_key).result == "fail"


def test_gnote_fails_numeric_mismatch(harness):
    _, _, evals, model, _ = harness
    claim_id, blob_key = _note_subject(
        harness, "Recommendation\nReserve KES 250,001.\nVerification"
    )
    response = _clean_note_response()
    response["numeric_claims"] = [
        {
            "text": "KES 250,001",
            "field_path": "reserve.total",
            "observed_value": "KES 250,001",
            "value_type": "money",
        }
    ]
    model.queue("g_note_grade", response)
    assert _note_grade(evals, claim_id, blob_key).result == "fail"


def test_gnote_fails_when_model_omits_numeric_token(harness):
    _, _, evals, model, _ = harness
    claim_id, blob_key = _note_subject(
        harness, "Recommendation\nReserve KES 250,000.\nVerification"
    )
    response = _clean_note_response()
    response["numeric_claims"] = []
    model.queue("g_note_grade", response)
    assert _note_grade(evals, claim_id, blob_key).result == "fail"


def test_t01_render_event_runs_gnote_consumer(harness):
    _, app, _, model, _ = harness
    claim_id, blob_key = _note_subject(
        harness,
        "Recommendation\nReserve KES 250,000. Loss date 12 July 2026.\nVerification",
    )
    model.queue("g_note_grade", _clean_note_response())
    _emit(
        app,
        "template.rendered",
        {
            "template_id": "T-01",
            "template_version": "fixture",
            "blob_key": blob_key,
            "signable": False,
        },
        claim_id,
    )
    _drain(app)
    with app.state.engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM grader_runs WHERE grader_id = 'G-NOTE'")
        ).scalar_one()
    assert count == 1


# --- correction capture + real corpus stats --------------------------------------


def test_approve_unchanged_interim_spelling_creates_no_case(harness):
    client, app, _, _, _ = harness
    claim_id = _claim(client)
    _resolved(
        app,
        claim_id,
        capability_id="intake.claim_creation",
        resolution="approved",
    )
    _drain(app)
    assert _cases(app) == []


def test_edited_resolution_captures_current_value_sources_tags_and_stats(harness):
    client, app, _, _, _ = harness
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, "corrected estimate")
    _human_write(
        client,
        claim_id,
        path="assessment.estimate_total",
        value=80_000_00,
        value_type="money",
    )
    source_event_id = _resolved(
        app,
        claim_id,
        capability_id="intake.claim_creation",
        resolution="edited",
        typed_changes=[{"path": "assessment.estimate_total", "kind": "money"}],
    )
    _drain(app)
    _drain(app)

    rows = _cases(app)
    assert len(rows) == 1
    case = rows[0]
    assert case["origin"] == "production_correction"
    assert case["input_bundle"]["source_event_id"] == source_event_id
    assert case["input_bundle"]["claim_id"] == claim_id
    assert len(case["input_bundle"]["documents"]) == 1
    document = case["input_bundle"]["documents"][0]
    assert document["document_id"] == document_id
    assert document["blob_ref"].startswith(f"documents/{claim_id}/")
    assert len(document["sha256"]) == 64
    assert case["expected"]["fields"] == {"assessment.estimate_total": 80_000_00}
    assert "capability:intake.claim_creation" in case["tags"]
    assert "failure_mode:edited" in case["tags"]
    assert "kind:money" in case["tags"]

    response = client.get("/eval/corpus/stats", headers=AGENT)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["by_origin"] == {"production_correction": 1}


def test_incomplete_correction_is_captured_blocked_never_guessed(harness):
    client, app, _, _, _ = harness
    claim_id = _claim(client)
    _resolved(
        app,
        claim_id,
        capability_id="pack.note_draft",
        resolution="rejected",
    )
    _drain(app)
    rows = _cases(app)
    assert len(rows) == 1
    capture = rows[0]["expected"]["_capture"]
    assert capture["status"] == "blocked_on_inputs"
    assert capture["missing_inputs"]
    assert rows[0]["expected"].get("fields", {}) == {}


# --- corpus execution, scorecards, weekly wiring ---------------------------------


def test_synthetic_batch_persists_test_case_grades_and_scorecards(harness):
    from eval_harness import CorpusObservation

    _, app, evals, model, executor = harness
    cite_claim, _ = _citation_field(
        harness,
        path="vehicle.reg",
        value="KDA 123B",
        value_type="string",
        label="KDA 123B corpus",
    )
    note_claim, note_blob = _note_subject(
        harness, "Recommendation\nReserve KES 250,001.\nVerification"
    )
    cite_case = evals.corpus.create_case(
        corpus="motor_v1",
        origin="seed_closed_claim",
        input_bundle={"fixture": "cite"},
        expected={"fields": {"vehicle.reg": "KDA 123B"}},
        tags=["capability:intake.claim_creation"],
    )
    note_case = evals.corpus.create_case(
        corpus="motor_v1",
        origin="seed_closed_claim",
        input_bundle={"fixture": "note"},
        expected={"note_rubric": {}},
        tags=["capability:pack.note_draft"],
    )
    executor.observations[cite_case] = [
        CorpusObservation(
            capability_id="intake.claim_creation",
            grader_id="G-CITE",
            subject_ref={"claim_id": cite_claim, "path": "vehicle.reg"},
        )
    ]
    executor.observations[note_case] = [
        CorpusObservation(
            capability_id="pack.note_draft",
            grader_id="G-NOTE",
            subject_ref={
                "claim_id": note_claim,
                "blob_key": note_blob,
                "template_id": "T-01",
            },
        )
    ]
    model.queue("g_cite_verify", {"value_present": True, "observed_value": None})
    note_response = _clean_note_response()
    note_response["numeric_claims"] = [
        {
            "text": "KES 250,001",
            "field_path": "reserve.total",
            "observed_value": "KES 250,001",
            "value_type": "money",
        }
    ]
    model.queue("g_note_grade", note_response)

    result = evals.corpus.run(corpus="motor_v1", capability_id=None, actor=ACTOR)
    assert result.total_cases == 2
    assert result.runnable_cases == 2
    assert result.blocked_cases == 0
    assert result.total_grades == 2
    assert result.scorecards["intake.claim_creation"] == {
        "cases": 1,
        "grades": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "blocked": 0,
        "pass_percent": 100,
    }
    assert result.scorecards["pack.note_draft"]["failed"] == 1
    with app.state.engine.connect() as connection:
        test_case_ids = set(
            connection.execute(
                text("SELECT test_case_id FROM grader_runs WHERE test_case_id IS NOT NULL")
            ).scalars()
        )
    assert test_case_ids == {cite_case, note_case}


def test_batch_counts_blocked_case_without_calling_executor(harness):
    _, _, evals, _, executor = harness
    evals.corpus.create_case(
        corpus="motor_v1",
        origin="production_correction",
        input_bundle={"source_event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        expected={
            "fields": {},
            "_capture": {
                "status": "blocked_on_inputs",
                "missing_inputs": ["corrected_path"],
            },
        },
        tags=["capability:pack.note_draft", "failure_mode:rejected"],
    )
    result = evals.corpus.run(corpus="motor_v1", capability_id=None, actor=ACTOR)
    assert result.total_cases == 1
    assert result.runnable_cases == 0
    assert result.blocked_cases == 1
    assert result.scorecards["pack.note_draft"]["blocked"] == 1
    assert executor.calls == []


def test_weekly_task_is_pack_configured_and_sync_drivable(harness):
    from claim_core import celery_app

    _, _, evals, _, _ = harness
    schedules = list(celery_app.conf.beat_schedule.values())
    weekly = [row for row in schedules if row.get("task") == "eval_harness.run_weekly_corpus"]
    assert len(weekly) == 1
    assert weekly[0]["schedule"] == 604_800
    result = evals.corpus.run_weekly(actor=ACTOR)
    assert result.corpus == "motor_v1"
    assert result.total_cases == 0


# --- anonymisation + exact percentage ratchet ------------------------------------


def test_anonymiser_is_claim_scoped_consistent_and_preserves_money():
    from eval_harness.anonymise import anonymise_bundle

    bundle = {
        "fields": [
            {
                "path": "parties.insured.name",
                "value": "Wanjiku Kamau",
                "value_type": "string",
                "pii_kind": "name",
            },
            {
                "path": "parties.driver.name",
                "value": "Wanjiku Kamau",
                "value_type": "string",
                "pii_kind": "name",
            },
            {
                "path": "parties.insured.national_id",
                "value": "12345678",
                "value_type": "string",
                "pii_kind": "id",
            },
            {
                "path": "parties.insured.phone",
                "value": "+254 712 345 678",
                "value_type": "string",
                "pii_kind": "phone",
            },
            {
                "path": "reserve.total",
                "value": 1_234_56,
                "value_type": "money",
                "pii_kind": None,
            },
        ]
    }
    first = anonymise_bundle(bundle, claim_key="claim-a", secret=b"test-secret")
    second = anonymise_bundle(bundle, claim_key="claim-a", secret=b"test-secret")
    other_claim = anonymise_bundle(bundle, claim_key="claim-b", secret=b"test-secret")
    values = [field["value"] for field in first["fields"]]
    assert values[0] == values[1]
    assert first == second
    assert values[0] != other_claim["fields"][0]["value"]
    assert values[-1] == 1_234_56
    serialised = json.dumps(first)
    for original in ("Wanjiku Kamau", "12345678", "+254 712 345 678"):
        assert original not in serialised
    assert "mapping" not in serialised.casefold()
    assert "test-secret" not in serialised


def test_anonymiser_refuses_unclassified_pii_whole_output():
    from eval_harness.anonymise import AnonymisationRefused, anonymise_bundle

    unsafe = {
        "fields": [
            {
                "path": "loss.narrative",
                "value": "Wanjiku Kamau called from +254712345678",
                "value_type": "string",
                "pii_class": "personal",
            }
        ]
    }
    with pytest.raises(AnonymisationRefused):
        anonymise_bundle(unsafe, claim_key="claim-a", secret=b"test-secret")


def test_autonomy_pass_percent_is_not_floor_truncated(harness):
    client, app, evals, _, _ = harness
    claim_id = _claim(client)
    for resolution in ("approved", "approved", "rejected"):
        _resolved(
            app,
            claim_id,
            capability_id="intake.claim_creation",
            resolution=resolution,
        )
    _drain(app)
    evidence = evals.autonomy.evidence("intake.claim_creation")
    assert evidence["rolling_20_pass_percent"] == pytest.approx(200 / 3)
