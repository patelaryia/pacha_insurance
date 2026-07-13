"""PACKET-05 acceptance — PRD-01 live stages and Packet-04 review ratchets.

Protected (CODEOWNERS): builders must not modify this file. The public contracts are
pinned by docs/packets/PACKET-05_docintel_live_model.md §3. Tests use injected model,
alert, and SDK fakes; no network or live Anthropic call is permitted.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

AGENT = {"X-Actor": "agent:doc_intel"}


class FixedClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeOcr:
    def __init__(self, words: list[dict] | None = None) -> None:
        self.calls = 0
        self.result = words or [
            {"text": "Broker", "bbox": [0.05, 0.05, 0.15, 0.09]},
            {"text": "reports", "bbox": [0.16, 0.05, 0.27, 0.09]},
            {"text": "KBX", "bbox": [0.28, 0.05, 0.34, 0.09]},
            {"text": "123A", "bbox": [0.35, 0.05, 0.43, 0.09]},
        ]

    def words(self, page_png: bytes) -> list[dict]:
        assert page_png
        self.calls += 1
        return [dict(word) for word in self.result]


class TaskModel:
    """Strict fake: every Packet-05 model call must declare its stable task."""

    def __init__(self, responses: dict[str, list[dict] | dict]) -> None:
        self.responses = {
            task: list(value) if isinstance(value, list) else [value]
            for task, value in responses.items()
        }
        self.calls: list[dict] = []

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        task = inputs["task"]
        self.calls.append({"tier": tier, "schema": schema, "inputs": dict(inputs)})
        queue = self.responses.get(task, [])
        if not queue:
            raise AssertionError(f"unexpected model task {task!r}")
        data = queue.pop(0)
        return {"data": data, "cost_usd": 0.01, "model_id": f"fake-{tier.lower()}"}


def _claim(client) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers=AGENT,
    )
    assert response.status_code == 201
    return response.json()["id"]


def _upload(client, claim_id: str, content: bytes, filename: str, mime: str) -> str:
    response = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (filename, io.BytesIO(content), mime)},
        data={"source_channel": "email", "source_ref": f"msg-{filename}"},
        headers=AGENT,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _pdf(pages: list[str]) -> bytes:
    import fitz

    document = fitz.open()
    for value in pages:
        page = document.new_page()
        page.insert_text((72, 72), value)
    return document.tobytes()


def _raster_pdf(page_count: int) -> bytes:
    import fitz
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 1200), "white")
    ImageDraw.Draw(image).text((50, 50), "SCANNED CLAIM PAGE", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()
    document = fitz.open()
    for _ in range(page_count):
        page = document.new_page(width=612, height=792)
        page.insert_image(page.rect, stream=png)
    return document.tobytes()


def _events(client, claim_id: str, event_type: str) -> list[dict]:
    timeline = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    return [event for event in timeline if event["type"] == event_type]


# --- Packet-04 review ratchets -----------------------------------------------------


@pytest.mark.parametrize(
    "source_ref",
    [
        None,
        {"document_id": "01K00000000000000000000000", "page": 1,
         "bbox": [0, 0, 1, 1], "anchor_text": "POL-123"},
    ],
)
def test_shared_write_gate_rejects_unresolved_or_nonexistent_citation(tmp_path, source_ref):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    client = TestClient(create_app(f"sqlite:///{tmp_path}/provenance.db"))
    claim_id = _claim(client)
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": "policy.number",
                    "value": "POL-123",
                    "value_type": "string",
                    "source_type": "extraction",
                    "source_ref": source_ref,
                    "confidence": 0.99,
                    "verification_state": "extracted",
                    "pii_class": "none",
                }
            ]
        },
        headers=AGENT,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CITATION_REQUIRED"
    hydrated = client.get(f"/claims/{claim_id}", headers=AGENT).json()
    assert "policy.number" not in hydrated["fields"]


def test_eml_subject_is_preserved_as_explicit_classify_input(tmp_path):
    from claim_core.storage import LocalBlobStore
    from doc_intel.normalize import normalise_document

    store = LocalBlobStore(tmp_path)
    store.put(
        "original.eml",
        b"From: broker@example.co.ke\r\nSubject: Accident KBX 123A\r\n\r\nBody only.",
    )
    result = normalise_document(
        document_id="01K00000000000000000000001",
        filename="intimation.eml",
        mime="message/rfc822",
        source_key="original.eml",
        blob_store=store,
        ocr_engine=FakeOcr(),
    )
    assert result.email_subject == "Accident KBX 123A"


def test_literal_forty_page_raster_scan_runs_ocr_on_every_page(tmp_path):
    from claim_core.storage import LocalBlobStore
    from doc_intel.normalize import normalise_document

    store = LocalBlobStore(tmp_path)
    store.put("scan.pdf", _raster_pdf(40))
    ocr = FakeOcr()
    result = normalise_document(
        document_id="01K00000000000000000000002",
        filename="scan.pdf",
        mime="application/pdf",
        source_key="scan.pdf",
        blob_store=store,
        ocr_engine=ocr,
    )
    assert result.page_count == 40
    assert ocr.calls == 40
    assert store.exists("pages/01K00000000000000000000002/40.png")


def test_photo_only_intimation_classifies_from_ocr(tmp_path):
    from fastapi.testclient import TestClient
    from PIL import Image

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    model = TaskModel(
        {
            "document_classify": {"doc_type": "intimation_email", "confidence": 0.96},
            "extract": {"fields": []},
        }
    )
    app = create_app(f"sqlite:///{tmp_path}/photo-intimation.db")
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=FixedClock())
    client = TestClient(app)
    claim_id = _claim(client)
    image = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(image, format="PNG")
    document_id = _upload(client, claim_id, image.getvalue(), "intimation.png", "image/png")
    outcome = app.state.doc_intel.process_document(document_id)
    assert outcome.stages["CLASSIFY"] == "succeeded"
    assert app.state.claim_service.get_document(document_id).doc_type == "intimation_email"


def test_duplicate_attachment_is_rejected_and_received_event_emits_once(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    app = create_app(f"sqlite:///{tmp_path}/dedupe.db")
    client = TestClient(app)
    claim_id = _claim(client)
    content = _pdf(["same immutable attachment"])
    first = _upload(client, claim_id, content, "one.pdf", "application/pdf")
    duplicate = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": ("renamed.pdf", io.BytesIO(content), "application/pdf")},
        data={"source_channel": "email", "source_ref": "msg-renamed.pdf"},
        headers=AGENT,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DUPLICATE_DOCUMENT"
    received = _events(client, claim_id, "document.received")
    assert [event["payload"]["document_id"] for event in received] == [first]


def test_every_pipeline_stage_has_one_named_celery_task():
    from doc_intel.tasks import PIPELINE_TASKS

    expected = {
        "NORMALIZE", "CLASSIFY", "SPLIT", "EXTRACT", "CITE", "VALIDATE",
        "COMMIT", "CONSISTENCY",
    }
    assert set(PIPELINE_TASKS) == expected
    assert {task.name for task in PIPELINE_TASKS.values()} == {
        f"doc_intel.{stage.casefold()}" for stage in expected
    }


# --- split detector and human boundaries ------------------------------------------


def test_low_document_classification_reaches_page_split_and_never_guesses(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    model = TaskModel(
        {
            "document_classify": {"doc_type": "other", "confidence": 0.40},
            "page_classify": [
                {"doc_type": "logbook", "confidence": 0.97},
                {"doc_type": "repair_estimate", "confidence": 0.98},
            ],
        }
    )
    app = create_app(f"sqlite:///{tmp_path}/split.db")
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=FixedClock())
    client = TestClient(app)
    claim_id = _claim(client)
    document_id = _upload(
        client, claim_id, _pdf(["logbook page", "estimate page"]),
        "bundle.pdf", "application/pdf",
    )
    outcome = app.state.doc_intel.process_document(document_id)
    review_types = {item["type"] for item in outcome.review_items}
    assert {"DOC_CLASSIFY", "DOC_SPLIT"} <= review_types
    assert [call["inputs"]["page"] for call in model.calls
            if call["inputs"]["task"] == "page_classify"] == [1, 2]
    assert outcome.stages["EXTRACT"] == "pending"
    assert outcome.committed_paths == []


def test_human_boundaries_create_children_reentering_at_classify(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine
    from doc_intel.llm import FakeModelClient

    app = create_app(f"sqlite:///{tmp_path}/children.db")
    build_engine(app, model_client=FakeModelClient([]), ocr_engine=FakeOcr())
    client = TestClient(app)
    claim_id = _claim(client)
    parent_id = _upload(
        client, claim_id, _pdf(["one", "two", "three", "four"]),
        "bundle.pdf", "application/pdf",
    )
    app.state.claim_service.set_document_status(parent_id, page_count=4)
    child_ids = app.state.doc_intel.apply_human_boundaries(
        parent_id,
        boundaries=[{"start_page": 1, "end_page": 2}, {"start_page": 3, "end_page": 4}],
        actor="user:01K00000000000000000000003",
    )
    assert len(child_ids) == 2
    with app.state.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, parent_document_id, doc_type, source FROM documents "
                 "WHERE parent_document_id = :parent ORDER BY id"),
            {"parent": parent_id},
        ).all()
    assert {row.parent_document_id for row in rows} == {parent_id}
    assert all(row.doc_type is None for row in rows)
    assert {event["payload"]["document_id"] for event in _events(
        client, claim_id, "document.received"
    )} >= set(child_ids)


def test_split_boundaries_must_cover_parent_exactly_once(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from claim_core.errors import ClaimCoreError
    from doc_intel.engine import build_engine
    from doc_intel.llm import FakeModelClient

    app = create_app(f"sqlite:///{tmp_path}/bad-boundary.db")
    build_engine(app, model_client=FakeModelClient([]), ocr_engine=FakeOcr())
    client = TestClient(app)
    claim_id = _claim(client)
    parent_id = _upload(client, claim_id, _pdf(["one", "two", "three"]),
                        "bundle.pdf", "application/pdf")
    app.state.claim_service.set_document_status(parent_id, page_count=3)
    with pytest.raises(ClaimCoreError) as captured:
        app.state.doc_intel.apply_human_boundaries(
            parent_id,
            boundaries=[{"start_page": 1, "end_page": 2},
                        {"start_page": 2, "end_page": 3}],
            actor="user:01K00000000000000000000004",
        )
    assert captured.value.status_code == 422
    assert captured.value.code == "INVALID_SPLIT_BOUNDARY"


# --- vision bbox and Swahili -------------------------------------------------------


def test_handwritten_vision_bbox_is_crop_verified_and_multiplied_by_point_nine(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    model = TaskModel(
        {
            "document_classify": {"doc_type": "claim_form", "confidence": 0.99},
            "extract": {
                "fields": [{"name": "policy_no", "value": "POL-123", "page": 1,
                            "confidence": 1.0, "citation_mode": "vision_bbox",
                            "bbox": [0.1, 0.1, 0.5, 0.2]}]
            },
            "vision_crop_verify": {"visible": True},
        }
    )
    app = create_app(f"sqlite:///{tmp_path}/vision.db")
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=FixedClock())
    client = TestClient(app)
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, _pdf(["handwritten claim form"]),
                          "claim-form.pdf", "application/pdf")
    outcome = app.state.doc_intel.process_document(document_id)
    assert "policy.number" in outcome.committed_paths
    field = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]["policy.number"]
    assert field["confidence"] == pytest.approx(0.855)  # 1.0 × 0.95 × 0.9
    assert field["source_ref"]["citation_mode"] == "vision_bbox"
    assert field["source_ref"]["vision_verified"] is True


def test_vision_bbox_on_sub_five_percent_nonhandwritten_page_is_verified(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    model = TaskModel(
        {
            "document_classify": {"doc_type": "repair_estimate", "confidence": 0.99},
            "extract": {
                "fields": [{"name": "total", "value": "KES 75,000", "page": 1,
                            "confidence": 1.0, "citation_mode": "vision_bbox",
                            "bbox": [0.1, 0.1, 0.5, 0.2]}]
            },
            "vision_crop_verify": {"visible": True},
        }
    )
    app = create_app(f"sqlite:///{tmp_path}/vision-eligible.db")
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=FixedClock())
    client = TestClient(app)
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, _pdf(["Grand Total: KES 75,000"]),
                          "estimate.pdf", "application/pdf")
    outcome = app.state.doc_intel.process_document(document_id)
    assert "assessment.estimate_total" in outcome.committed_paths
    assert any(call["inputs"]["task"] == "vision_crop_verify" for call in model.calls)


def test_swahili_gloss_is_derived_narrative_never_rule_input(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    model = TaskModel(
        {
            "document_classify": {"doc_type": "police_abstract", "confidence": 0.98},
            "extract": {"fields": [{"name": "remarks", "value": "Gari liligongana",
                                     "anchor_text": "Gari liligongana", "page": 1,
                                     "confidence": 0.98,
                                     "citation_mode": "anchor_text"}]},
            "translate_swahili_gloss": {"gloss": "The vehicle collided"},
        }
    )
    app = create_app(f"sqlite:///{tmp_path}/swahili.db")
    build_engine(app, model_client=model, ocr_engine=FakeOcr(), clock=FixedClock())
    client = TestClient(app)
    claim_id = _claim(client)
    document_id = _upload(client, claim_id, _pdf(["Remarks: Gari liligongana"]),
                          "abstract.pdf", "application/pdf")
    app.state.doc_intel.process_document(document_id)
    payload = json.loads(app.state.blob_store.get(
        f"derived/{document_id}/remarks_gloss.json"
    ))
    assert payload == {
        "source_field": "remarks",
        "value": "The vehicle collided",
        "machine_translated": True,
        "rule_input": False,
        "status": "pending_field_registration",
    }


# --- consistency, real adapter seam, and SLOs -------------------------------------


def test_consistency_cc1_to_cc4_are_exact_and_cc2_never_substitutes_years_driving():
    from doc_intel.consistency import evaluate_observations

    observations = {
        "claim": {"reg": "KBX 123A", "insured_name": "Jane Wanjiku",
                  "loss_date": "2026-07-01"},
        "logbook": {"reg": "KBX 123A", "owner_name": "Jane Wanjiku"},
        "repair_estimate": {"reg": "KBY 999Z"},
        "assessor_report": {"reg": "KBX 123A"},
        "driving_licence": {"dob": "1988-04-12", "expiry": "2026-06-30"},
        "claim_form": {"driver_dob": "1988-04-12", "years_driving": 8},
    }
    results = {row["check_id"]: row for row in evaluate_observations(observations)}
    assert results["CC-1"]["status"] == "inconsistent"
    assert results["CC-1"]["severity"] == "block-pack"
    assert results["CC-2"]["status"] == "insufficient"
    assert results["CC-2"]["review_required"] is True
    assert results["CC-3"]["status"] == "inconsistent"
    assert results["CC-3"]["severity"] == "block-pack"
    assert results["CC-4"]["status"] == "consistent"


def test_cc5_nonconsistent_always_flags_never_blocks_and_is_capped_l2():
    from doc_intel.consistency import evaluate_cc5

    model = TaskModel(
        {"consistency_cc5": {"status": "inconsistent", "rationale": "front vs rear",
                             "score": 0.12}}
    )
    result = evaluate_cc5(
        narrative="Rear impact",
        photo_descriptions=["Front bumper damage"],
        model_client=model,
    )
    assert result["review_type"] == "CONSISTENCY_FLAG"
    assert result["blocks"] is False
    assert result["auto_clear"] is False
    assert result["max_autonomy_level"] == "L2"


def test_anthropic_adapter_uses_config_tool_schema_zero_temperature_and_metering():
    from doc_intel.anthropic_client import AnthropicModelClient

    class Messages:
        def __init__(self) -> None:
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            tool = SimpleNamespace(type="tool_use", input={"doc_type": "logbook",
                                                            "confidence": 0.97})
            return SimpleNamespace(content=[tool], usage=SimpleNamespace(
                input_tokens=1_000, output_tokens=200), model=kwargs["model"])

    messages = Messages()
    sdk = SimpleNamespace(messages=messages)
    config = {
        "tiers": {
            "MODEL_LIGHT": {"model_id": "configured-light",
                            "input_usd_per_mtok": "1.00",
                            "output_usd_per_mtok": "5.00"}
        }
    }
    adapter = AnthropicModelClient(sdk, config=config, ledger=None)
    schema = {"type": "object", "required": ["doc_type", "confidence"],
              "properties": {"doc_type": {"type": "string"},
                             "confidence": {"type": "number"}}}
    result = adapter.structured_call(
        tier="MODEL_LIGHT", schema=schema,
        inputs={"task": "document_classify", "filename": "x.pdf"},
    )
    assert messages.kwargs["model"] == "configured-light"
    assert messages.kwargs["temperature"] == 0
    assert messages.kwargs["tools"][0]["input_schema"] == schema
    assert messages.kwargs["tool_choice"]["type"] == "tool"
    assert result["cost_usd"] == pytest.approx(0.002)


def test_slo_sentinel_persists_samples_and_alerts_each_individual_breach():
    from doc_intel.telemetry import SloSentinel

    samples: list[dict] = []
    alerts: list[tuple[str, dict]] = []

    class Sink:
        def alert(self, code: str, payload: dict) -> None:
            alerts.append((code, payload))

    sentinel = SloSentinel(
        duration_limit_ms=180_000,
        cost_limit_usd=Decimal("0.60"),
        sample_sink=samples.append,
        alert_sink=Sink(),
    )
    sentinel.record(document_id="doc-1", duration_ms=180_001,
                    cost_usd=Decimal("0.61"))
    assert samples[0]["breached_duration"] is True
    assert samples[0]["breached_cost"] is True
    assert {code for code, _payload in alerts} == {
        "DOC_INTEL_DURATION_BREACH", "DOC_INTEL_COST_BREACH"
    }
