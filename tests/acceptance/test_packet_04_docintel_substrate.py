"""PACKET-04 acceptance — PRD-01 §1.2–§1.5 deterministic substrate + ED-4a wrapper.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-04_docintel_substrate.md §3 (engine, stage⇄model data shapes,
blob-store exposure). No live LLM calls — fakes implement the ModelClient protocol.
"""
from __future__ import annotations

import io
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

AGENT = {"X-Actor": "agent:intake"}


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 20, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class FakeModel:
    """Deterministic ModelClient: queued classify/extract results per tier."""

    def __init__(self) -> None:
        self.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
        self.extract_fields: list[dict] = []
        self.calls: list[str] = []

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        self.calls.append(tier)
        if tier == "MODEL_LIGHT":
            return {"data": dict(self.classify_result), "cost_usd": 0.001,
                    "model_id": "fake-light"}
        return {"data": {"fields": [dict(f) for f in self.extract_fields]},
                "cost_usd": 0.01, "model_id": "fake-heavy"}


class FakeOcr:
    def __init__(self) -> None:
        self.calls = 0
        self.result = [{"text": "OCR-WORD", "bbox": [0.1, 0.1, 0.3, 0.15]}]

    def words(self, page_png: bytes) -> list[dict]:
        self.calls += 1
        return [dict(w) for w in self.result]


@pytest.fixture()
def harness(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app
    from doc_intel.engine import build_engine

    clock = MutableClock()
    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc4.db")
    app = create_app(url, clock=clock)
    model = FakeModel()
    ocr = FakeOcr()
    build_engine(app, model_client=model, ocr_engine=ocr, clock=clock)
    return TestClient(app), app, clock, model, ocr


def _claim(client) -> str:
    r = client.post(
        "/claims", json={"lob": "motor", "pack_version": "motor@1.3.0"}, headers=AGENT
    )
    assert r.status_code == 201
    return r.json()["id"]


def _upload(client, claim_id, content: bytes, filename: str, mime: str) -> str:
    r = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": (filename, io.BytesIO(content), mime)},
        data={"source_channel": "email", "source_ref": f"msg-{filename}"},
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _drain(app, clock, cycles: int = 12) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break
        clock.advance(seconds=90)


def _pdf(pages: list[str]) -> bytes:
    import fitz

    doc = fitz.open()
    for content in pages:
        page = doc.new_page()
        page.insert_text((72, 72), content, fontsize=11)
    return doc.tobytes()


def _stage_status(app, document_id: str) -> dict[str, str]:
    with app.state.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT stage, status FROM document_stages "
                "WHERE document_id = :d"
            ),
            {"d": document_id},
        )
        return {stage: status for stage, status in rows}


def _events(client, claim_id, event_type):
    tl = client.get(f"/claims/{claim_id}/timeline", headers=AGENT).json()["events"]
    return [e for e in tl if e["type"] == event_type]


# --- NORMALIZE ---------------------------------------------------------------------


def test_eml_normalized_with_renders_and_text_layer(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "intimation_email", "confidence": 0.95}
    model.extract_fields = []
    claim_id = _claim(client)
    eml = (
        b"From: broker@example.co.ke\r\nTo: claims@mayfair.co.ke\r\n"
        b"Subject: Accident KBX 123A\r\n\r\n"
        b"Insured Jane Wanjiku vehicle KBX 123A collided on Mombasa Road."
    )
    doc_id = _upload(client, claim_id, eml, "intimation.eml", "message/rfc822")
    _drain(app, clock)

    assert _stage_status(app, doc_id)["NORMALIZE"] == "succeeded"
    store = app.state.blob_store
    assert store.exists(f"pages/{doc_id}/1.png")
    text_layer = store.get(f"text/{doc_id}/1.json").decode("utf-8")
    assert "KBX" in text_layer  # real text layer from the rendered PDF


def test_image_normalized_to_pdf(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "photo_damage", "confidence": 0.95}
    claim_id = _claim(client)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (120, 10, 10)).save(buf, format="JPEG")
    doc_id = _upload(client, claim_id, buf.getvalue(), "damage.jpg", "image/jpeg")
    _drain(app, clock)

    assert _stage_status(app, doc_id)["NORMALIZE"] == "succeeded"
    assert app.state.blob_store.exists(f"pages/{doc_id}/1.png")


def test_xlsx_kept_native_with_csv_snapshot(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
    model.extract_fields = []
    claim_id = _claim(client)
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["description", "qty", "unit_price", "amount"])
    ws.append(["Bumper", 1, 45000, 45000])
    buf = io.BytesIO()
    wb.save(buf)
    doc_id = _upload(
        client, claim_id, buf.getvalue(), "estimate.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    _drain(app, clock)

    assert _stage_status(app, doc_id)["NORMALIZE"] == "succeeded"
    store = app.state.blob_store
    csv_keys = [k for k in store.list_keys(f"snapshots/{doc_id}/") if k.endswith(".csv")]
    assert csv_keys, "CSV snapshot artifact missing"
    assert b"Bumper" in store.get(csv_keys[0])


def test_ocr_fallback_below_five_percent_coverage(harness):
    client, app, clock, model, ocr = harness
    model.classify_result = {"doc_type": "photo_damage", "confidence": 0.95}
    claim_id = _claim(client)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (255, 255, 255)).save(buf, format="PNG")
    doc_id = _upload(client, claim_id, buf.getvalue(), "scan.png", "image/png")
    _drain(app, clock)

    assert ocr.calls >= 1  # no native text layer → OCR fallback ran
    text_layer = app.state.blob_store.get(f"text/{doc_id}/1.json").decode("utf-8")
    assert "OCR-WORD" in text_layer


def test_corrupt_pdf_rejected_loudly(harness):
    client, app, clock, _model, _ocr = harness
    claim_id = _claim(client)
    doc_id = _upload(
        client, claim_id, b"%PDF-1.4 not really a pdf \x00\x01garbage",
        "broken.pdf", "application/pdf",
    )
    _drain(app, clock)

    with app.state.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM documents WHERE id = :d"), {"d": doc_id}
        ).scalar()
    assert status == "rejected"
    assert _events(client, claim_id, "document.rejected")
    reviews = _events(client, claim_id, "review.created")
    assert any(
        e["payload"].get("subtype") == "doc_normalize_failed" for e in reviews
    )


def test_forty_page_scan_completes(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "photo_damage", "confidence": 0.95}
    claim_id = _claim(client)
    doc_id = _upload(
        client, claim_id, _pdf([f"Page {n} content" for n in range(1, 41)]),
        "bundle.pdf", "application/pdf",
    )
    _drain(app, clock)

    assert _stage_status(app, doc_id)["NORMALIZE"] == "succeeded"
    store = app.state.blob_store
    assert store.exists(f"pages/{doc_id}/40.png")
    with app.state.engine.connect() as conn:
        page_count = conn.execute(
            text("SELECT page_count FROM documents WHERE id = :d"), {"d": doc_id}
        ).scalar()
    assert page_count == 40


# --- end-to-end commit with citations ------------------------------------------------


ESTIMATE_TEXT = (
    "Kamau Motors Garage\n"
    "Bumper replacement qty 1 unit 45,000 amount 45,000\n"
    "Respray rear panel qty 1 unit 30,000 amount 30,000\n"
    "Grand Total: KES 75,000"
)


def test_end_to_end_extraction_commit_with_bbox_provenance(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
    model.extract_fields = [
        {"name": "total", "value": "KES 75,000",
         "anchor_text": "Grand Total: KES 75,000", "page": 1, "confidence": 0.97},
    ]
    claim_id = _claim(client)
    doc_id = _upload(client, claim_id, _pdf([ESTIMATE_TEXT]), "est.pdf", "application/pdf")
    _drain(app, clock)

    stages = _stage_status(app, doc_id)
    for stage in ("NORMALIZE", "CLASSIFY", "EXTRACT", "CITE", "VALIDATE", "COMMIT"):
        assert stages[stage] == "succeeded", (stage, stages)
    assert stages["SPLIT"] == "skipped"
    assert stages["CONSISTENCY"] == "skipped"

    field = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"][
        "assessment.estimate_total"
    ]
    assert field["value"] == 75_000_00  # money_kes: shillings ×100 → integer cents
    assert isinstance(field["value"], int)
    assert field["source_type"] == "extraction"
    assert field["verification_state"] == "extracted"
    ref = field["source_ref"]
    assert ref["document_id"] == doc_id
    assert ref["page"] == 1
    assert len(ref["bbox"]) == 4
    assert ref["anchor_text"] == "Grand Total: KES 75,000"
    assert _events(client, claim_id, "document.extracted")


def test_citation_failure_forces_review_never_commit(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
    model.extract_fields = [
        {"name": "total", "value": "KES 75,000",
         "anchor_text": "THIS ANCHOR DOES NOT EXIST ANYWHERE", "page": 1,
         "confidence": 0.99},
    ]
    claim_id = _claim(client)
    _upload(client, claim_id, _pdf([ESTIMATE_TEXT]), "est2.pdf", "application/pdf")
    _drain(app, clock)

    fields = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]
    assert "assessment.estimate_total" not in fields  # zero provenance, zero commit
    reviews = [
        e for e in _events(client, claim_id, "review.created")
        if e["payload"].get("type") == "FIELD_VERIFY"
    ]
    assert len(reviews) == 1
    assert reviews[0]["payload"]["combined_confidence"] == 0


def test_below_threshold_goes_to_field_verify(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
    model.extract_fields = [
        {"name": "total", "value": "KES 75,000",
         "anchor_text": "Grand Total: KES 75,000", "page": 1, "confidence": 0.89},
    ]
    claim_id = _claim(client)
    _upload(client, claim_id, _pdf([ESTIMATE_TEXT]), "est3.pdf", "application/pdf")
    _drain(app, clock)

    # 0.89 × 1.0 = 0.89 < 0.90 money threshold → review, not committed
    fields = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]
    assert "assessment.estimate_total" not in fields
    assert any(
        e["payload"].get("type") == "FIELD_VERIFY"
        for e in _events(client, claim_id, "review.created")
    )


def test_threshold_boundary_inclusive_commit(harness):
    client, app, clock, model, _ocr = harness
    model.classify_result = {"doc_type": "repair_estimate", "confidence": 0.95}
    model.extract_fields = [
        {"name": "total", "value": "KES 75,000",
         "anchor_text": "Grand Total: KES 75,000", "page": 1, "confidence": 0.90},
    ]
    claim_id = _claim(client)
    _upload(client, claim_id, _pdf([ESTIMATE_TEXT]), "est4.pdf", "application/pdf")
    _drain(app, clock)

    # 0.90 × 1.0 = 0.90 == threshold → commits (>= is binding, register #38)
    fields = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"]
    assert fields["assessment.estimate_total"]["value"] == 75_000_00


# --- validators (§1.3 — binding behaviours) -------------------------------------------


def test_money_kes_parses_variants_to_integer_cents():
    from doc_intel.validators import money_kes

    assert money_kes("KES 1,234,567").value == 123_456_700
    assert money_kes("KSh 15,000").value == 1_500_000
    assert money_kes("1,234.50").value == 123_450  # explicit cents parsed
    assert money_kes("75,000").value == 7_500_000
    for parsed in (money_kes("KES 1,234,567"), money_kes("1,234.50")):
        assert isinstance(parsed.value, int)
    assert money_kes("no money here").outcome == "fail"


def test_sum_check_tolerance_exactly_one_shilling():
    from doc_intel.validators import sum_check

    lines = [45_000_00, 30_000_00]
    assert sum_check(lines, 75_000_00).outcome == "pass"
    assert sum_check(lines, 75_001_00).outcome == "pass"      # off by exactly KES 1
    assert sum_check(lines, 75_001_01).outcome == "fail"      # beyond tolerance


def test_kenya_reg_full_pattern_set():
    from doc_intel.validators import kenya_reg

    assert kenya_reg("KBX 123A").outcome == "pass"      # standard
    assert kenya_reg("KBX123A").outcome == "pass"       # optional space
    assert kenya_reg("KAB 123").outcome == "pass"       # pre-2000 legacy
    assert kenya_reg("KMEA 123B").outcome == "pass"     # motorcycle
    assert kenya_reg("ZD 1234").outcome == "pass"       # trailer
    assert kenya_reg("GK 123A").outcome == "pass"       # government
    assert kenya_reg("12CD 34K").outcome == "out_of_scope"   # diplomatic → review
    assert kenya_reg("BANANA").outcome == "out_of_scope"     # never auto-match


def test_kra_pin_pattern():
    from doc_intel.validators import kra_pin

    assert kra_pin("A012345678Z").outcome == "pass"
    assert kra_pin("P987654321K").outcome == "pass"
    assert kra_pin("B012345678Z").outcome == "fail"


# --- schema registry + prompt generation ----------------------------------------------


def test_prompt_generated_from_schema(harness):
    _client, app, _clock, _model, _ocr = harness
    engine = app.state.doc_intel
    prompt = engine.registry.prompt_for("repair_estimate")
    assert "garage_name" in prompt
    assert "anchor_text" in prompt  # §1.4 anchor requirement embedded
    assert "page" in prompt
    with pytest.raises(LookupError):
        engine.registry.prompt_for("medical_invoice_v9")  # unknown → never guess


def test_all_eleven_motor_doc_types_registered(harness):
    _client, app, _clock, _model, _ocr = harness
    expected = {
        "intimation_email", "claim_form", "logbook", "driving_licence",
        "kra_pin_cert", "police_abstract", "repair_estimate", "assessor_report",
        "discharge_voucher", "bank_discharge_letter", "photo_damage",
    }
    assert expected <= set(app.state.doc_intel.registry.doc_types())


# --- ED-4a wrapper semantics -----------------------------------------------------------


def test_wrapper_schema_invalid_regenerates_exactly_once_then_raises():
    from doc_intel.llm import ModelSchemaError, ModelWrapper

    calls = {"n": 0}

    class AlwaysInvalid:
        def structured_call(self, *, tier, schema, inputs):
            calls["n"] += 1
            return {"data": {"unexpected": "shape"}, "cost_usd": 0.001,
                    "model_id": "fake"}

    wrapper = ModelWrapper(AlwaysInvalid())
    with pytest.raises(ModelSchemaError):
        wrapper.structured_call(
            tier="MODEL_LIGHT",
            schema={"type": "object", "required": ["doc_type"],
                    "properties": {"doc_type": {"type": "string"}}},
            inputs={},
        )
    assert calls["n"] == 2  # one regeneration, exactly


def test_wrapper_budget_breach_raises_immediately_no_retry():
    from doc_intel.llm import ModelBudgetExceeded, ModelWrapper

    calls = {"n": 0}

    class CheapModel:
        def structured_call(self, *, tier, schema, inputs):
            calls["n"] += 1
            return {"data": {"doc_type": "logbook", "confidence": 0.9},
                    "cost_usd": 0.40, "model_id": "fake"}

    wrapper = ModelWrapper(CheapModel(), budget_ceiling_usd=0.60)
    schema = {"type": "object", "required": ["doc_type"],
              "properties": {"doc_type": {"type": "string"}}}
    wrapper.structured_call(tier="MODEL_LIGHT", schema=schema, inputs={})  # spent 0.40
    wrapper.structured_call(tier="MODEL_LIGHT", schema=schema, inputs={})  # spent 0.80 ≥ ceiling
    with pytest.raises(ModelBudgetExceeded):
        wrapper.structured_call(tier="MODEL_LIGHT", schema=schema, inputs={})
    assert calls["n"] == 2  # once breached, no further live calls — ever
