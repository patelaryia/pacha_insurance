"""Exhaustive deterministic unit coverage for the Packet-04 substrate."""

from __future__ import annotations

import io
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest


def test_kenya_registration_boundaries_and_normalisation():
    from doc_intel.validators import kenya_reg

    passing = {
        "kbx 123a": "KBX 123A",
        "KAB123": "KAB123",
        "KMEA 123B": "KMEA 123B",
        "ZD1234": "ZD1234",
        "GK K 123A": "GK K 123A",
        "GK123": "GK123",
    }
    for raw, normalised in passing.items():
        result = kenya_reg(raw)
        assert result.outcome == "pass"
        assert result.value == normalised
    assert kenya_reg(123).outcome == "out_of_scope"
    assert kenya_reg("CD 123A").outcome == "out_of_scope"
    assert kenya_reg("").outcome == "not_applicable"


def test_money_date_phone_and_conservative_licence_validators():
    from doc_intel.validators import date_past, licence_no, money_kes, phone_ke

    assert money_kes(25).value == 2_500
    assert money_kes("KES. 1,000.05").value == 100_005
    assert money_kes("1.001").outcome == "fail"
    assert money_kes(True).outcome == "fail"
    assert money_kes(None).outcome == "not_applicable"
    today = date(2026, 7, 10)
    assert date_past("2026-07-10", today=today).value == "2026-07-10"
    assert date_past("2026-07-11", today=today).outcome == "fail"
    assert date_past("not-a-date", today=today).outcome == "fail"
    assert phone_ke("0712 345 678").value == "254712345678"
    assert phone_ke("712345678").outcome == "out_of_scope"
    assert phone_ke("0201234567").outcome == "out_of_scope"
    assert licence_no("DL 1234567").outcome == "out_of_scope"


def test_kra_pin_sum_check_and_validator_registry_failures():
    from doc_intel.validators import kra_pin, sum_check, validate_field

    assert kra_pin(" a012345678z ").value == "A012345678Z"
    assert kra_pin(None).outcome == "not_applicable"
    assert sum_check([{"amount": 100_00}, {"amount": 50_00}], 151_00).outcome == "pass"
    assert sum_check([100_00], 101_01).outcome == "fail"
    assert sum_check("not-lines", 100_00).outcome == "fail"
    with pytest.raises(ValueError, match="unknown validator"):
        validate_field("invented", "value")


def test_confidence_outcomes_and_threshold_categories():
    from doc_intel.confidence import combined_confidence, threshold_for

    assert combined_confidence(0.9, "pass").as_tuple().exponent == -2
    assert combined_confidence(1, "fail") == combined_confidence(1, "out_of_scope")
    assert threshold_for({"validator": "money_kes"}).is_signed() is False
    assert str(threshold_for({"validator": "money_kes"})) == "0.90"
    assert str(threshold_for({"validator": "date_past"})) == "0.90"
    assert str(threshold_for({"validator": "kenya_reg"})) == "0.90"
    assert str(threshold_for({"validator": "not_applicable"})) == "0.85"
    assert str(threshold_for({"confidence_threshold": 0.77})) == "0.77"
    with pytest.raises(ValueError, match="unknown validator outcome"):
        combined_confidence(1, "invented")


def test_anchor_matching_inclusive_threshold_and_bbox_union():
    from doc_intel.citations import match_anchor

    words = [
        {"text": "Grand", "bbox": [0.1, 0.2, 0.2, 0.3]},
        {"text": "Total:", "bbox": [0.21, 0.2, 0.3, 0.3]},
        {"text": "KES", "bbox": [0.31, 0.2, 0.4, 0.3]},
        {"text": "75,000", "bbox": [0.41, 0.2, 0.55, 0.3]},
    ]
    match = match_anchor("Grand Total: KES 75,000", words)
    assert match is not None
    assert match.bbox == [0.1, 0.2, 0.55, 0.3]
    assert match_anchor("missing anchor", words) is None
    assert match_anchor("x" * 121, words) is None
    assert match_anchor("Grand", [{"text": "Grand", "bbox": [0, 1]}]) is None
    boundary = match_anchor(
        "aaaaaaaaaaaaaaaaaaaa",
        [{"text": "bbbaaaaaaaaaaaaaaaaa", "bbox": [0, 0, 1, 1]}],
    )
    assert boundary is not None
    assert boundary.score == 0.85


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 10, tzinfo=UTC)

    def __call__(self):
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_model_wrapper_retries_switches_fallback_and_meters_invalid_calls():
    from doc_intel.llm import FakeModelClient, FlakyModelClient, ModelWrapper

    response = {"data": {"ok": True}, "cost_usd": 0.1, "model_id": "fallback"}
    fake = FakeModelClient([response])
    flaky = FlakyModelClient(fake, failures=3)
    clock = _Clock()
    wrapper = ModelWrapper(
        flaky,
        config={
            "sleep": clock.sleep,
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "primary",
                    "fallback_model_id": "fallback",
                }
            },
        },
        clock=clock,
    )
    result = wrapper.structured_call(
        tier="MODEL_LIGHT",
        schema={"type": "object", "required": ["ok"]},
        inputs={"payload": 1},
    )
    assert result["model_id"] == "fallback"
    assert fake.calls[0]["inputs"]["_model_id"] == "fallback"
    assert clock.now == datetime(2026, 7, 10, tzinfo=UTC) + timedelta(seconds=7)
    assert wrapper.spent_usd == 0.1


def test_model_wrapper_transport_exhaustion_and_fake_queue():
    from doc_intel.llm import (
        FakeModelClient,
        ModelTransportError,
        ModelUnavailable,
        ModelWrapper,
    )

    clock = _Clock()
    fake = FakeModelClient([ModelTransportError("down")] * 2)
    wrapper = ModelWrapper(
        fake,
        config={"max_attempts": 2, "sleep": clock.sleep},
        clock=clock,
    )
    with pytest.raises(ModelUnavailable):
        wrapper.structured_call(tier="MODEL_LIGHT", schema={"type": "object"}, inputs={})


def test_registry_strict_target_paths_and_deterministic_prompt(tmp_path):
    from claim_core import FieldDefinition
    from doc_intel.registry import SchemaRegistry

    registry = SchemaRegistry({"known.path": FieldDefinition("string", "none")})
    registry.register(
        {
            "doc_type": "known",
            "fields": {
                "field": {
                    "type": "string",
                    "required": True,
                    "validator": "not_applicable",
                    "pii_class": "none",
                    "target_path": "known.path",
                    "description": "Known field",
                    "example": "value",
                }
            },
        }
    )
    assert registry.doc_types() == ("known",)
    assert "anchor_text" in registry.prompt_for("known")
    assert registry.extraction_output_schema("known")["properties"]["fields"]
    with pytest.raises(KeyError, match="unknown document type"):
        registry.schema_for("unknown")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.schema_for("known"))
    bad = SchemaRegistry({})
    with pytest.raises(ValueError, match="unregistered target_path"):
        bad.register(
            {
                "doc_type": "bad",
                "fields": {
                    "field": {
                        "type": "string",
                        "required": False,
                        "validator": "not_applicable",
                        "pii_class": "none",
                        "target_path": "missing.path",
                    }
                },
            }
        )
    (tmp_path / "empty.yaml").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        bad.load_directory(tmp_path)


def test_prepare_commit_never_writes_without_citation(tmp_path):
    from claim_core.storage import LocalBlobStore
    from doc_intel.commit import prepare_commit

    store = LocalBlobStore(tmp_path)
    schema = {
        "fields": {
            "amount": {
                "type": "money",
                "validator": "money_kes",
                "pii_class": "none",
                "target_path": "assessment.estimate_total",
            }
        }
    }
    base = {
        "name": "amount",
        "normalised_value": 75_000_00,
        "combined_confidence": 0.99,
        "threshold": 0.90,
        "validator_outcome": "pass",
        "page": 1,
        "anchor_text": "Total KES 75,000",
        "citation": None,
        "citation_failed": True,
    }
    writes, reviews = prepare_commit(
        document_id="doc",
        doc_type="estimate",
        fields=[base],
        schema=schema,
        blob_store=store,
        review_capability_id="doc.extract",
    )
    assert writes == []
    assert reviews[0]["combined_confidence"] == 0.99
    assert reviews[0]["capability_id"] == "doc.extract"
    assert reviews[0]["value_type"] == "money"
    assert reviews[0]["page"] == 1
    cited = {**base, "citation": {"bbox": [0, 0, 1, 1]}, "citation_failed": False}
    writes, reviews = prepare_commit(
        document_id="doc",
        doc_type="estimate",
        fields=[cited],
        schema=schema,
        blob_store=store,
        review_capability_id="doc.extract",
    )
    assert [write.path for write in writes] == ["assessment.estimate_total"]
    assert reviews == []


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary absent")
def test_tesseract_adapter_returns_normalized_word_boxes():
    from PIL import Image, ImageDraw, ImageFont

    from doc_intel.normalize import TesseractOcrEngine

    image = Image.new("RGB", (600, 180), "white")
    ImageDraw.Draw(image).text(
        (20, 60),
        "PACHA CLAIM",
        fill="black",
        font=ImageFont.load_default(size=48),
        stroke_width=1,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    words = TesseractOcrEngine().words(buffer.getvalue())
    assert words
    assert "PACHA" in " ".join(word["text"].upper() for word in words)
    for word in words:
        assert all(0 <= coordinate <= 1 for coordinate in word["bbox"])


def test_dictionary_extension_rejects_override(tmp_path):
    from claim_core.dictionary import register_dictionary_extensions

    extension = Path(tmp_path) / "fields.yaml"
    extension.write_text(
        "fields:\n  policy.number:\n    value_type: money\n    pii_class: none\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="may not override"):
        register_dictionary_extensions(extension)


def test_blob_store_exists_and_lists_keys(tmp_path):
    from claim_core.storage import LocalBlobStore

    store = LocalBlobStore(tmp_path)
    store.put("pages/doc/2.png", b"two")
    store.put("pages/doc/1.png", b"one")
    assert store.get("pages/doc/1.png") == b"one"
    assert store.exists("pages/doc/2.png")
    assert not store.exists("pages/doc/3.png")
    assert store.list_keys("pages/doc") == ["pages/doc/1.png", "pages/doc/2.png"]
    assert store.list_keys("") == ["pages/doc/1.png", "pages/doc/2.png"]
    with pytest.raises(ValueError, match="escapes"):
        store.exists("../outside")


def test_vehicle_registration_remains_plaintext_per_ed6a(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from claim_core import register_dictionary_extensions
    from claim_core.app import create_app

    register_dictionary_extensions("packs/motor/fields.yaml")
    app = create_app(f"sqlite:///{tmp_path}/plate.db")
    client = TestClient(app)
    headers = {"X-Actor": "agent:doc_intel"}
    claim_id = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers=headers,
    ).json()["id"]
    document_id = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": ("logbook.txt", b"KBX 123A", "text/plain")},
        data={"source_channel": "test", "source_ref": "logbook"},
        headers=headers,
    ).json()["id"]
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={
            "writes": [
                {
                    "path": "vehicle.reg",
                    "value": "KBX 123A",
                    "value_type": "string",
                    "source_type": "extraction",
                    "source_ref": {
                        "document_id": document_id,
                        "page": 1,
                        "bbox": [0, 0, 1, 1],
                        "anchor_text": "KBX 123A",
                    },
                    "confidence": 0.99,
                    "verification_state": "extracted",
                    "pii_class": "personal-low",
                }
            ]
        },
        headers=headers,
    )
    assert response.status_code == 200
    with app.state.engine.connect() as connection:
        stored = connection.execute(
            text("SELECT value FROM claim_fields WHERE claim_id = :claim_id"),
            {"claim_id": claim_id},
        ).scalar_one()
    assert "KBX 123A" in stored
    hydrated = client.get(f"/claims/{claim_id}", headers=headers).json()
    assert hydrated["fields"]["vehicle.reg"]["value"] == "KBX 123A"
