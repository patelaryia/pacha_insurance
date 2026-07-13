"""Regression coverage for the Packet-05 CTO blocking findings."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session


class _AlertSink:
    def __init__(self) -> None:
        self.calls = []

    def alert(self, code, payload) -> None:
        self.calls.append((code, payload))


def _alert_factory():
    return _AlertSink()


def _database_url(tmp_path, name: str) -> str:
    return os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")


def _claim_and_document(app, content=b"source"):
    from claim_core.schemas import ClaimCreate

    service = app.state.claim_service
    claim = service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.3.0"), "agent:test"
    )
    document = service.add_document(
        claim.id,
        filename="source.pdf",
        mime="application/pdf",
        content=content,
        source_channel="test",
        source_ref="regression",
        actor="agent:test",
    )
    return claim, document


def test_anthropic_sdk_receives_real_multimodal_blocks_and_audit_is_safe():
    from doc_intel.anthropic_client import AnthropicModelClient

    png = b"\x89PNG\r\n\x1a\nactual-image"

    class Messages:
        requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(
                content=[{"type": "tool_use", "input": {"ok": True}}],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                model="model-a",
            )

    class Audit:
        details = []

        def record_model_call(self, detail):
            self.details.append(detail)

    messages, audit = Messages(), Audit()
    client = AnthropicModelClient(
        SimpleNamespace(messages=messages),
        config={
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "model-a",
                    "input_usd_per_mtok": "1",
                    "output_usd_per_mtok": "1",
                }
            },
            "audit_redacted_keys": ["page_text"],
        },
        ledger=audit,
    )
    contracts = [
        {
            "task": "document_classify",
            "first_text": "claim",
            "filename": "claim.pdf",
            "email_subject": "subject",
            "source": {"channel": "email"},
            "page_png": png,
        },
        {"task": "page_classify", "page_text": "PII", "page_png": png},
        {"task": "extract", "document_text": "claim", "page_pngs": [png, png]},
        {"task": "vision_crop_verify", "value": "secret", "crop_png": png},
    ]
    for inputs in contracts:
        client.structured_call(
            tier="MODEL_LIGHT", schema={"type": "object"}, inputs=inputs
        )
    provider_blocks = [request["messages"][0]["content"] for request in messages.requests]
    assert [[block["type"] for block in blocks] for blocks in provider_blocks] == [
        ["text", "image"],
        ["text", "image"],
        ["text", "image", "image"],
        ["text", "image"],
    ]
    assert [contract["task"] in blocks[0]["text"] for contract, blocks in zip(
        contracts, provider_blocks, strict=True
    )] == [True] * 4
    assert all(blocks[1]["source"]["data"] for blocks in provider_blocks)
    encoded_audit = json.dumps(audit.details)
    assert "actual-image" not in encoded_audit
    assert audit.details[1]["request"]["page_text"] == "__redacted__"
    assert audit.details[0]["request"]["page_png"]["size_bytes"] == len(png)


def test_model_audit_is_event_first_then_single_writer_ledger(tmp_path):
    from claim_core.app import create_app
    from claim_core.models import AuditLedgerRow, Event
    from doc_intel.anthropic_client import AnthropicModelClient

    app = create_app(_database_url(tmp_path, "audit"))
    claim, _document = _claim_and_document(app)

    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[{"type": "tool_use", "input": {"ok": True}}],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                model=kwargs["model"],
            )

    client = AnthropicModelClient(
        SimpleNamespace(messages=Messages()),
        config={
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "model-a",
                    "input_usd_per_mtok": "1",
                    "output_usd_per_mtok": "1",
                }
            }
        },
        ledger=app.state.claim_service,
    )
    client.structured_call(
        tier="MODEL_LIGHT",
        schema={"type": "object"},
        inputs={
            "task": "test",
            "_claim_id": claim.id,
            "page_png": b"\x89PNG\r\n\x1a\nprivate-image",
        },
    )
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditLedgerRow)) == 0
        event = session.scalar(select(Event).where(Event.type == "model.called"))
        assert event is not None
        assert "private-image" not in json.dumps(event.payload)
    app.state.dispatcher.dispatch_once({"ledger"})
    with Session(app.state.engine) as session:
        rows = list(
            session.scalars(
                select(AuditLedgerRow).where(
                    AuditLedgerRow.action == "model.structured_call"
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].detail["event_type"] == "model.called"


def test_stage_acquisition_is_atomic_and_crash_recovery_is_explicit(tmp_path, monkeypatch):
    from claim_core.app import create_app
    from doc_intel.engine import build_engine
    from doc_intel.llm import FakeModelClient
    from doc_intel.stages import StageResult

    app = create_app(_database_url(tmp_path, "stage"))
    model = FakeModelClient(
        [{"data": {"ok": True}, "cost_usd": 0.0, "model_id": "fake"}]
    )
    engine = build_engine(app, model_client=model)
    _claim, document = _claim_and_document(app)
    engine._ensure_stages(document.id)
    calls = []

    def run_stage(_document, stage):
        calls.append(stage)
        model.structured_call(tier="MODEL_LIGHT", schema={}, inputs={"task": "atomic"})
        return StageResult(status="succeeded")

    monkeypatch.setattr(engine, "_run_stage", run_stage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.process_stage(document.id, "NORMALIZE"), range(2)))
    assert calls == ["NORMALIZE"]
    assert len(model.calls) == 1
    assert {row["status"] for row in results} <= {"running", "succeeded"}

    engine._finish_stage(document.id, "CLASSIFY", StageResult(status="running"))
    assert engine.recover_stage(document.id, "CLASSIFY", actor="system") is True
    assert engine._stage_rows(document.id)["CLASSIFY"].status == "pending"
    monkeypatch.setattr(
        engine, "_run_stage", lambda _document, _stage: StageResult(status="succeeded")
    )
    assert engine.process_stage(document.id, "CLASSIFY")["status"] == "succeeded"


def test_extraction_and_consistency_dedupe_inside_claim_transaction(tmp_path):
    from claim_core.app import create_app
    from claim_core.models import ClaimField, ConsistencyResult, Event
    from claim_core.schemas import FieldWrite

    app = create_app(_database_url(tmp_path, "claim-lock"))
    claim, document = _claim_and_document(app)
    write = FieldWrite(
        path="policy.number",
        value="POL-1",
        value_type="string",
        source_type="extraction",
        source_ref={
            "document_id": document.id,
            "page": 1,
            "bbox": [0.1, 0.1, 0.5, 0.2],
            "anchor_text": "POL-1",
        },
        confidence=0.9,
        verification_state="extracted",
    )
    service = app.state.claim_service
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: service.write_document_extractions(
                    claim.id, document.id, [write], "agent:doc_intel"
                ),
                range(2),
            )
        )
    result = {
        "check_id": "CC-1",
        "status": "inconsistent",
        "severity": "flag",
        "rationale": "test",
        "evidence": {"a": 1},
    }
    def store_consistency(_index):
        return service.append_consistency_result(
            claim.id,
            result,
            input_fingerprint="fingerprint",
            review_payload={
                "type": "CONSISTENCY_FLAG",
                "subtype": "CC-1",
                "document_id": document.id,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stored = list(
            pool.map(store_consistency, range(2))
        )
    assert sorted(stored) == [False, True]
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(ClaimField)) == 1
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.type == "field.updated")
        ) == 1
        assert session.scalar(select(func.count()).select_from(ConsistencyResult)) == 1
        assert session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.type == "review.created")
        ) == 1


def test_migration_0005_preserves_pinned_columns_and_builds_active_dialect_index(
    tmp_path,
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    url = _database_url(tmp_path, "migration")
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "platform/claim_core/alembic"),
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    database = create_engine(url)
    inspector = inspect(database)
    assert "input_fingerprint" not in {
        column["name"] for column in inspector.get_columns("consistency_results")
    }
    with database.connect() as connection:
        if database.dialect.name == "postgresql":
            index_names = set(
                connection.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = "
                    "'consistency_results'"
                ).scalars()
            )
        else:
            index_names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'consistency_results'"
                ).scalars()
            )
    assert "uq_consistency_results_input" in index_names
    stage_checks = " ".join(
        str(check.get("sqltext", ""))
        for check in inspector.get_check_constraints("document_stages")
    )
    assert "paused" in stage_checks
    database.dispose()
    command.downgrade(config, "0004_docintel_live_stages")
    command.upgrade(config, "head")


def test_exact_vision_eligibility_boundary_and_canonical_split_actor(tmp_path):
    import fitz

    from claim_core import ClaimCoreError
    from claim_core.app import create_app
    from doc_intel.engine import build_engine
    from doc_intel.llm import FakeModelClient
    from doc_intel.vision import eligible

    assert eligible(handwritten=False, text_coverage=0.049) is True
    assert eligible(handwritten=False, text_coverage=0.05) is False
    assert eligible(handwritten=True, text_coverage=1.0) is True

    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "one")
    pdf.new_page().insert_text((72, 72), "two")
    content = pdf.tobytes()
    pdf.close()

    class Ocr:
        def words(self, _png):
            return []

    app = create_app(_database_url(tmp_path, "actor"))
    engine = build_engine(app, model_client=FakeModelClient([]), ocr_engine=Ocr())
    _claim, parent = _claim_and_document(app, content)
    app.state.claim_service.set_document_status(parent.id, page_count=2)
    boundaries = [
        {"start_page": 1, "end_page": 1},
        {"start_page": 2, "end_page": 2},
    ]
    for actor in ("agent:doc_intel", "system", "user:not-a-ulid", ""):
        with pytest.raises(ClaimCoreError, match="user:<ULID>"):
            engine.apply_human_boundaries(
                parent.id, boundaries=boundaries, actor=actor
            )
    actor = "user:01K00000000000000000000003"
    first = engine.apply_human_boundaries(parent.id, boundaries=boundaries, actor=actor)
    second = engine.apply_human_boundaries(parent.id, boundaries=boundaries, actor=actor)
    assert second == first


def test_native_text_coverage_is_only_word_bbox_area_divided_by_page_area():
    import fitz

    from doc_intel.normalize import _native_words

    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((72, 72), "Grand Total KES 75,000")
    raw_words = page.get_text("words")
    expected = sum(
        max(0.0, word[2] - word[0]) * max(0.0, word[3] - word[1])
        for word in raw_words
    ) / (600 * 800)
    words, coverage = _native_words(page)
    pdf.close()
    assert coverage == pytest.approx(expected)
    assert coverage < 0.05
    assert {word["source"] for word in words} == {"native"}


def test_operational_runtime_requires_alerts_and_chains_only_success(tmp_path, monkeypatch):
    from claim_core.app import create_app
    from doc_intel.engine import build_engine
    from doc_intel.llm import FakeModelClient, ModelUnavailable
    from doc_intel.stages import StageResult

    app = create_app(_database_url(tmp_path, "worker"))
    with pytest.raises(RuntimeError, match="alert sink"):
        build_engine(app, model_client=FakeModelClient([]), runtime_mode="worker")

    class Scheduler:
        def __init__(self):
            self.calls = []

        def schedule(self, document_id, stage):
            self.calls.append((document_id, stage))

    scheduler = Scheduler()
    engine = build_engine(
        app,
        model_client=FakeModelClient([]),
        runtime_mode="worker",
        stage_scheduler=scheduler,
        alert_sink=_AlertSink(),
    )
    _claim, document = _claim_and_document(app)
    engine.consume(SimpleNamespace(type="document.received", payload={"document_id": document.id}))
    assert scheduler.calls == [(document.id, "NORMALIZE")]

    monkeypatch.setattr(
        engine, "_run_stage", lambda _document, _stage: StageResult(status="succeeded")
    )
    engine.process_stage(document.id, "NORMALIZE", schedule_next=True)
    assert scheduler.calls[-1] == (document.id, "CLASSIFY")

    second = _claim_and_document(app, b"second")[1]

    fail_calls = 0

    def fail(_document, _stage):
        nonlocal fail_calls
        fail_calls += 1
        raise ModelUnavailable("paused")

    monkeypatch.setattr(engine, "_run_stage", fail)
    assert engine.process_stage(second.id, "NORMALIZE", schedule_next=True)["status"] == "paused"
    assert engine.process_stage(second.id, "NORMALIZE", schedule_next=True)["status"] == "paused"
    assert fail_calls == 1
    assert scheduler.calls[-1] != (second.id, "CLASSIFY")
    assert engine.recover_stage(second.id, "NORMALIZE", actor="system") is True
    monkeypatch.setattr(
        engine, "_run_stage", lambda _document, _stage: StageResult(status="succeeded")
    )
    assert engine.process_stage(second.id, "NORMALIZE")["status"] == "succeeded"


def test_failed_schema_calls_are_included_in_document_cost_sample(tmp_path):
    import fitz

    from claim_core.app import create_app
    from claim_core.models import DocIntelSample, Event
    from doc_intel.anthropic_client import AnthropicModelClient
    from doc_intel.engine import build_engine

    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "classification source")
    content = pdf.tobytes()
    pdf.close()
    app = create_app(_database_url(tmp_path, "failed-cost"))

    class Messages:
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                content=[{"type": "tool_use", "input": {"invalid": True}}],
                usage=SimpleNamespace(input_tokens=0, output_tokens=40_000),
                model=kwargs["model"],
            )

    messages = Messages()
    adapter = AnthropicModelClient(
        SimpleNamespace(messages=messages),
        config={
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "light",
                    "input_usd_per_mtok": "1",
                    "output_usd_per_mtok": "10",
                }
            }
        },
        ledger=app.state.claim_service,
    )
    alerts = _AlertSink()
    engine = build_engine(app, model_client=adapter, alert_sink=alerts)
    _claim, document = _claim_and_document(app, content)
    outcome = engine.process_document(document.id)
    assert outcome.stages["CLASSIFY"] == "failed"
    assert messages.calls == 2
    with Session(app.state.engine) as session:
        sample = session.scalar(
            select(DocIntelSample).where(DocIntelSample.document_id == document.id)
        )
        assert sample is not None
        assert sample.cost_usd == Decimal("0.800000")
        assert sample.breached_cost is True
        assert session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.type == "model.called",
                Event.payload["document_id"].as_string() == document.id,
            )
        ) == 2
    assert [code for code, _payload in alerts.calls] == ["DOC_INTEL_COST_BREACH"]


def test_fresh_worker_runtime_constructs_from_environment_with_injected_sdk(
    tmp_path, monkeypatch
):
    from doc_intel.runtime import build_worker_runtime

    monkeypatch.setenv("DATABASE_URL", _database_url(tmp_path, "fresh-worker"))
    monkeypatch.setenv("PACHA_BLOB_ROOT", str(tmp_path / "blobs"))
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: None))
    runtime = build_worker_runtime(sdk_client=sdk, alert_sink=_AlertSink())
    assert runtime.engine.runtime_mode == "worker"
    assert runtime.engine.model_client.sdk_client is sdk
    assert runtime.engine.stage_scheduler.queue == "doc_intel"


def test_worker_bootstrap_fail_loud_paths_and_configured_scheduler(
    tmp_path, monkeypatch
):
    from doc_intel import runtime as runtime_module
    from doc_intel import tasks

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PACHA_BLOB_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        runtime_module.build_worker_runtime(
            sdk_client=SimpleNamespace(), alert_sink=_AlertSink()
        )
    with pytest.raises(RuntimeError, match="module:attribute"):
        runtime_module._load_factory("invalid")
    with pytest.raises(RuntimeError, match="queue"):
        runtime_module.CeleryStageScheduler("")

    scheduled = []

    class Task:
        def apply_async(self, *, args, queue):
            scheduled.append((args, queue))

    monkeypatch.setitem(tasks.PIPELINE_TASKS, "NORMALIZE", Task())
    runtime_module.CeleryStageScheduler("configured-q").schedule("doc-1", "NORMALIZE")
    assert scheduled == [(["doc-1"], "configured-q")]

    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: None))
    monkeypatch.setenv("DATABASE_URL", _database_url(tmp_path, "factory-worker"))
    monkeypatch.setenv("PACHA_BLOB_ROOT", str(tmp_path / "factory-blobs"))
    monkeypatch.setenv("DOC_INTEL_ALERT_SINK_FACTORY", f"{__name__}:_alert_factory")
    monkeypatch.setattr(runtime_module, "build_anthropic_sdk_client", lambda: sdk)
    built = runtime_module.build_worker_runtime()
    assert isinstance(built.engine.sentinel.alert_sink, _AlertSink)


def test_slo_samples_append_and_only_individual_breaches_alert():
    from doc_intel.telemetry import SloSentinel

    samples, alerts = [], _AlertSink()
    sentinel = SloSentinel(
        duration_limit_ms=100,
        cost_limit_usd=Decimal("0.50"),
        sample_sink=samples.append,
        alert_sink=alerts,
    )
    sentinel.record(document_id="a", duration_ms=100, cost_usd=Decimal("0.50"))
    sentinel.record(document_id="b", duration_ms=101, cost_usd=Decimal("0.51"))
    assert len(samples) == 2
    assert [code for code, _payload in alerts.calls] == [
        "DOC_INTEL_DURATION_BREACH",
        "DOC_INTEL_COST_BREACH",
    ]


def test_pending_fallback_ends_in_controlled_provider_unavailable():
    from doc_intel.anthropic_client import AnthropicModelClient
    from doc_intel.llm import ModelUnavailable, ModelWrapper

    class Messages:
        calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs["model"])
            error = ConnectionError("down")
            raise error

    messages = Messages()
    client = AnthropicModelClient(
        SimpleNamespace(messages=messages),
        config={
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "primary",
                    "fallback_model_id": "pending_capture",
                    "input_usd_per_mtok": "1",
                    "output_usd_per_mtok": "1",
                }
            }
        },
        ledger=None,
    )
    wrapper = ModelWrapper(
        client,
        config={
            "max_attempts": 2,
            "max_elapsed_seconds": 2,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "fallback_after_attempt": 1,
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "primary",
                    "fallback_model_id": "pending_capture",
                }
            },
            "sleep": lambda _seconds: None,
        },
    )
    with pytest.raises(ModelUnavailable):
        wrapper.structured_call(
            tier="MODEL_LIGHT", schema={"type": "object"}, inputs={"task": "test"}
        )
    assert messages.calls == ["primary"]


def test_configured_fallback_model_is_used_after_primary_transport_failure():
    from doc_intel.anthropic_client import AnthropicModelClient
    from doc_intel.llm import ModelWrapper

    class Messages:
        calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs["model"])
            if len(self.calls) == 1:
                raise ConnectionError("primary down")
            return SimpleNamespace(
                content=[{"type": "tool_use", "input": {"ok": True}}],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                model=kwargs["model"],
            )

    messages = Messages()
    prices = {
        "model_id": "primary",
        "fallback_model_id": "fallback",
        "input_usd_per_mtok": "1",
        "output_usd_per_mtok": "1",
    }
    client = AnthropicModelClient(
        SimpleNamespace(messages=messages),
        config={"tiers": {"MODEL_LIGHT": prices}},
        ledger=None,
    )
    wrapper = ModelWrapper(
        client,
        config={
            "max_attempts": 2,
            "max_elapsed_seconds": 2,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "fallback_after_attempt": 1,
            "tiers": {"MODEL_LIGHT": prices},
            "sleep": lambda _seconds: None,
        },
    )
    result = wrapper.structured_call(
        tier="MODEL_LIGHT",
        schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        inputs={"task": "test"},
    )
    assert result["model_id"] == "fallback"
    assert messages.calls == ["primary", "fallback"]
