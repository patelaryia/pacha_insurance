"""Document-intelligence pipeline orchestration and application wiring."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from claim_core import (
    Base,
    BlobStore,
    ClaimCoreError,
    ClaimService,
    HumanOverrideProtected,
    field_dictionary,
    new_ulid,
    register_dictionary_extensions,
)
from doc_intel.citations import match_anchor
from doc_intel.commit import commit_writes, prepare_commit
from doc_intel.confidence import combined_confidence, threshold_for
from doc_intel.consistency import evaluate_cc5, evaluate_observations, load_definitions
from doc_intel.llm import (
    ModelBudgetExceeded,
    ModelClient,
    ModelSchemaError,
    ModelUnavailable,
    ModelWrapper,
)
from doc_intel.normalize import (
    NormaliseError,
    OcrEngine,
    TesseractOcrEngine,
    normalise_document,
)
from doc_intel.registry import SchemaRegistry
from doc_intel.settings import DEFAULTS
from doc_intel.split import pdf_subset, validate_boundaries
from doc_intel.stages import (
    STAGES,
    TERMINAL_STAGE_STATUSES,
    DocumentStage,
    PipelineOutcome,
    StageResult,
)
from doc_intel.swahili import create_remarks_gloss
from doc_intel.telemetry import LoggingAlertSink, SloSentinel
from doc_intel.validators import money_kes, sum_check, validate_field
from doc_intel.vision import (
    crop_png,
    eligible,
    normalized_bbox,
    verify_crop,
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "required": ["doc_type", "confidence"],
    "additionalProperties": False,
    "properties": {
        "doc_type": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class PipelinePause(RuntimeError):
    """A visible review or provider condition paused resumable processing."""


class DocIntelEngine:
    """Synchronous-drivable implementation of the PRD-01 stage pipeline."""

    def __init__(
        self,
        app: FastAPI,
        *,
        model_client: ModelClient,
        ocr_engine: OcrEngine | None,
        clock: Callable[[], datetime] | None,
        model_config: Mapping[str, Any] | None = None,
        alert_sink: Any | None = None,
        runtime_mode: str = "test",
        stage_scheduler: Any | None = None,
    ) -> None:
        self.app = app
        self.engine: Engine = app.state.engine
        self.claim_service: ClaimService = app.state.claim_service
        self.blob_store: BlobStore = app.state.blob_store
        self.clock = clock or utc_now
        self.ocr_engine = ocr_engine or TesseractOcrEngine()
        self.model_client = model_client
        self.runtime_mode = runtime_mode
        self.stage_scheduler = stage_scheduler
        root = Path(__file__).resolve().parents[2]
        pack_config = yaml.safe_load(
            (root / "packs" / "motor" / "doc_intel.yaml").read_text(encoding="utf-8")
        )
        self.model_config = {**DEFAULTS["model_wrapper"], **pack_config}
        if model_config is not None:
            self.model_config.update(dict(model_config))
        self.review_capability_id = self.model_config.get("review_capability_id")
        if not isinstance(self.review_capability_id, str) or not self.review_capability_id.strip():
            raise ValueError("doc-intel review_capability_id must be configured")
        self.review_capability_id = self.review_capability_id.strip()
        register_dictionary_extensions(root / "packs" / "motor" / "fields.yaml")
        self.registry = SchemaRegistry(field_dictionary())
        self.registry.load_directory(Path(__file__).with_name("schemas") / "motor")
        Base.metadata.create_all(self.engine, tables=[DocumentStage.__table__])
        slo = self.model_config["slo"]
        if runtime_mode != "test" and alert_sink is None:
            raise RuntimeError("operational doc-intel requires an alert sink")
        self.sentinel = SloSentinel(
            duration_limit_ms=int(slo["duration_limit_ms"]),
            cost_limit_usd=Decimal(str(slo["cost_limit_usd"])),
            sample_sink=self.claim_service.append_doc_intel_sample,
            alert_sink=alert_sink if alert_sink is not None else LoggingAlertSink(),
        )

    def consume(self, event: Any) -> None:
        """Transactional-outbox consumer entry; unrelated event types are no-ops."""

        if event.type != "document.received":
            return
        document_id = event.payload.get("document_id")
        if isinstance(document_id, str):
            if self.runtime_mode == "test":
                self.process_document(document_id)
            else:
                if self.stage_scheduler is None:
                    raise RuntimeError("operational doc-intel requires a stage scheduler")
                self.stage_scheduler.schedule(document_id, "NORMALIZE")

    def _document(self, document_id: str) -> dict[str, Any]:
        row = self.claim_service.get_document(document_id)
        return {
            "id": row.id,
            "claim_id": row.claim_id,
            "parent_document_id": row.parent_document_id,
            "doc_type": row.doc_type,
            "status": row.status,
            "filename": row.filename,
            "mime": row.mime,
            "s3_key": row.s3_key,
            "page_count": row.page_count,
            "source": row.source,
        }

    def _ensure_stages(self, document_id: str) -> None:
        now = self.clock()
        with Session(self.engine) as session, session.begin():
            existing = set(
                session.scalars(
                    select(DocumentStage.stage).where(
                        DocumentStage.document_id == document_id
                    )
                )
            )
            for stage in STAGES:
                if stage not in existing:
                    session.add(
                        DocumentStage(
                            id=new_ulid(),
                            document_id=document_id,
                            stage=stage,
                            status="pending",
                            attempts=0,
                            last_error=None,
                            output_ref=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )

    def _stage_rows(self, document_id: str) -> dict[str, DocumentStage]:
        with Session(self.engine) as session:
            rows = list(
                session.scalars(
                    select(DocumentStage)
                    .where(DocumentStage.document_id == document_id)
                    .order_by(DocumentStage.created_at)
                )
            )
            for row in rows:
                session.expunge(row)
        return {row.stage: row for row in rows}

    def _begin_stage(self, document_id: str, stage: str) -> bool:
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(DocumentStage).where(
                    DocumentStage.document_id == document_id,
                    DocumentStage.stage == stage,
                    DocumentStage.status == "pending",
                )
                .values(
                    status="running",
                    attempts=DocumentStage.attempts + 1,
                    last_error=None,
                    updated_at=self.clock(),
                )
            )
            return result.rowcount == 1

    def recover_stage(self, document_id: str, stage: str, *, actor: str) -> bool:
        """Explicitly release a crashed running stage; never auto-reclaim uncertain work."""

        if actor != "system" and re.fullmatch(r"user:[0-9A-HJKMNP-TV-Z]{26}", actor) is None:
            raise ClaimCoreError(
                422, "VALUE_TYPE_MISMATCH", "Recovery requires system or user ULID"
            )
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(DocumentStage)
                .where(
                    DocumentStage.document_id == document_id,
                    DocumentStage.stage == stage,
                    DocumentStage.status.in_(("running", "failed", "paused")),
                )
                .values(
                    status="pending",
                    last_error=f"explicit recovery by {actor}",
                    updated_at=self.clock(),
                )
            )
            return result.rowcount == 1

    def _pause_stage(self, document_id: str, stage: str, error: Exception) -> None:
        self._finish_stage(
            document_id,
            stage,
            StageResult(status="paused", last_error=f"{type(error).__name__}: {error}"[:2000]),
        )

    def _finish_stage(self, document_id: str, stage: str, result: StageResult) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(DocumentStage).where(
                    DocumentStage.document_id == document_id,
                    DocumentStage.stage == stage,
                )
            )
            if row is None:
                raise RuntimeError(f"missing durable stage {stage}")
            row.status = result.status
            row.last_error = result.last_error
            row.output_ref = result.output_ref
            row.updated_at = self.clock()

    def _fail_stage(self, document_id: str, stage: str, error: Exception) -> None:
        self._finish_stage(
            document_id,
            stage,
            StageResult(status="failed", last_error=f"{type(error).__name__}: {error}"[:2000]),
        )

    def _store_output(self, document_id: str, stage: str, output: dict[str, Any]) -> str:
        key = f"pipeline/{document_id}/{stage.casefold()}.json"
        self.blob_store.put(
            key,
            json.dumps(output, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )
        return key

    def _load_output(self, document_id: str, stage: str) -> dict[str, Any]:
        rows = self._stage_rows(document_id)
        key = rows[stage].output_ref
        if key is None or not self.blob_store.exists(key):
            raise RuntimeError(f"stage {stage} has no durable output")
        return json.loads(self.blob_store.get(key))

    def _model_wrapper(self, document_id: str) -> ModelWrapper:
        """Restore the durable per-document spend accumulator before a model call."""

        wrapper = ModelWrapper(
            self.model_client,
            budget_ceiling_usd=self.model_config.get("budget_ceiling_usd"),
            config=self.model_config,
        )
        wrapper.spent_usd = float(self._model_cost(document_id))
        return wrapper

    def _model_cost(self, document_id: str) -> Decimal:
        document = self._document(document_id)
        return sum(
            (
                Decimal(str(event.payload.get("detail", {}).get("cost_usd", "0")))
                for event in self.claim_service.timeline(document["claim_id"])
                if event.type == "model.called"
                and event.payload.get("document_id") == document_id
            ),
            start=Decimal("0"),
        )

    def _event_exists(self, claim_id: str, event_type: str, identity: dict[str, Any]) -> bool:
        for event in self.claim_service.timeline(claim_id):
            if event.type == event_type and all(
                event.payload.get(key) == value for key, value in identity.items()
            ):
                return True
        return False

    def _emit_once(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        identity: dict[str, Any],
        document_status: str | None = None,
    ) -> None:
        if self._event_exists(claim_id, event_type, identity):
            if document_status is not None:
                self.claim_service.set_document_status(
                    payload["document_id"], status=document_status
                )
            return
        if document_status is not None:
            self.claim_service.set_document_status(
                payload["document_id"], status=document_status
            )
        with Session(self.engine) as session, session.begin():
            self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor="agent:doc_intel",
                correlation_id=new_ulid(),
            )

    def _review_once(self, claim_id: str, payload: dict[str, Any]) -> None:
        identity = {
            key: payload[key]
            for key in ("type", "subtype", "document_id", "path")
            if key in payload
        }
        self._emit_once(
            claim_id=claim_id,
            event_type="review.created",
            payload=payload,
            identity=identity,
        )

    def _normalise(self, document: dict[str, Any]) -> StageResult:
        try:
            result = normalise_document(
                document_id=document["id"],
                filename=document["filename"],
                mime=document["mime"],
                source_key=document["s3_key"],
                blob_store=self.blob_store,
                ocr_engine=self.ocr_engine,
                text_coverage_threshold=float(
                    self.model_config["vision_text_coverage_threshold"]
                ),
            )
        except NormaliseError:
            self._emit_once(
                claim_id=document["claim_id"],
                event_type="document.rejected",
                payload={"document_id": document["id"], "reason": "normalise_failed"},
                identity={"document_id": document["id"]},
                document_status="rejected",
            )
            self._review_once(
                document["claim_id"],
                {
                    "type": "EXCEPTION",
                    "subtype": "doc_normalize_failed",
                    "document_id": document["id"],
                },
            )
            raise
        self.claim_service.set_document_status(
            document["id"], page_count=result.page_count
        )
        output = {
            "pdf_key": result.pdf_key,
            "page_count": result.page_count,
            "text_keys": result.text_keys,
            "page_keys": result.page_keys,
            "first_text": result.first_text,
            "page_text_coverages": result.page_text_coverages,
            "email_subject": result.email_subject,
        }
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "NORMALIZE", output),
            output=output,
        )

    def _classify(self, document: dict[str, Any]) -> StageResult:
        normalised = self._load_output(document["id"], "NORMALIZE")
        wrapper = self._model_wrapper(document["id"])
        result = wrapper.structured_call(
            tier="MODEL_LIGHT",
            schema=CLASSIFY_SCHEMA,
            inputs={
                "task": "document_classify",
                "_claim_id": document["claim_id"],
                "_document_id": document["id"],
                "filename": document["filename"],
                "first_text": normalised["first_text"],
                "page_png": self.blob_store.get(normalised["page_keys"][0]),
                "email_subject": normalised.get("email_subject"),
                "source": document["source"],
            },
        )
        output = {
            **result["data"],
            "_model": {"cost_usd": result["cost_usd"], "model_id": result["model_id"]},
        }
        doc_type = output["doc_type"]
        confidence = output["confidence"]
        requires_review = (
            doc_type == "other"
            or doc_type not in self.registry.doc_types()
            or confidence < float(self.model_config["classification_confidence_threshold"])
        )
        if requires_review:
            self._review_once(
                document["claim_id"],
                {
                    "type": "DOC_CLASSIFY",
                    "document_id": document["id"],
                    "candidate_doc_type": doc_type,
                    "confidence": confidence,
                },
            )
        else:
            self.claim_service.set_document_status(
                document["id"], doc_type=doc_type, status="classified"
            )
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "CLASSIFY", output),
            output=output,
        )

    def _split(self, document: dict[str, Any]) -> StageResult:
        classified = self._load_output(document["id"], "CLASSIFY")
        normalised = self._load_output(document["id"], "NORMALIZE")
        threshold = float(self.model_config["classification_confidence_threshold"])
        needs_pages = (
            classified["doc_type"] == "other"
            or classified["doc_type"] not in self.registry.doc_types()
            or float(classified["confidence"]) < threshold
            or int(normalised["page_count"]) > 4
        )
        if not needs_pages:
            output = {"page_results": [], "split_required": False}
            return StageResult(
                status="skipped",
                output_ref=self._store_output(document["id"], "SPLIT", output),
                output=output,
            )
        wrapper = self._model_wrapper(document["id"])
        starting_cost = wrapper.spent_usd
        page_results = []
        for page, (page_key, text_key) in enumerate(
            zip(normalised["page_keys"], normalised["text_keys"], strict=True), start=1
        ):
            words = json.loads(self.blob_store.get(text_key))
            result = wrapper.structured_call(
                tier="MODEL_LIGHT",
                schema=CLASSIFY_SCHEMA,
                inputs={
                    "task": "page_classify",
                    "_claim_id": document["claim_id"],
                    "_document_id": document["id"],
                    "page": page,
                    "page_png": self.blob_store.get(page_key),
                    "page_text": " ".join(str(word["text"]) for word in words)[:2_000],
                    "filename": document["filename"],
                },
            )
            page_results.append(result["data"])
        usable = all(
            row["doc_type"] in self.registry.doc_types()
            and float(row["confidence"]) >= threshold
            for row in page_results
        )
        classes = {row["doc_type"] for row in page_results}
        split_required = not usable or len(classes) != 1
        output = {
            "page_results": page_results,
            "split_required": split_required,
            "_model": {"cost_usd": wrapper.spent_usd - starting_cost},
        }
        output_ref = self._store_output(document["id"], "SPLIT", output)
        if split_required:
            self._review_once(
                document["claim_id"],
                {"type": "DOC_SPLIT", "document_id": document["id"]},
            )
            return StageResult(
                status="failed",
                output_ref=output_ref,
                output=output,
                last_error="human split boundaries required",
            )
        resolved_type = next(iter(classes))
        self.claim_service.set_document_status(
            document["id"], doc_type=resolved_type, status="classified"
        )
        return StageResult(status="succeeded", output_ref=output_ref, output=output)

    def _extract(self, document: dict[str, Any]) -> StageResult:
        classified = self._load_output(document["id"], "CLASSIFY")
        normalised = self._load_output(document["id"], "NORMALIZE")
        doc_type = self._document(document["id"])["doc_type"] or classified["doc_type"]
        wrapper = self._model_wrapper(document["id"])
        result = wrapper.structured_call(
            tier="MODEL_HEAVY",
            schema=self.registry.extraction_output_schema(doc_type),
            inputs={
                "task": "extract",
                "_claim_id": document["claim_id"],
                "_document_id": document["id"],
                "prompt": self.registry.prompt_for(doc_type),
                "document_text": "\n".join(
                    " ".join(str(word["text"]) for word in json.loads(self.blob_store.get(key)))
                    for key in normalised["text_keys"]
                ),
                "page_pngs": [self.blob_store.get(key) for key in normalised["page_keys"]],
            },
        )
        output = {
            **result["data"],
            "_model": {"cost_usd": result["cost_usd"], "model_id": result["model_id"]},
        }
        if doc_type == "police_abstract":
            remarks = next(
                (
                    field.get("value")
                    for field in output.get("fields", [])
                    if field.get("name") == "remarks" and isinstance(field.get("value"), str)
                ),
                None,
            )
            if remarks:
                gloss_cost = create_remarks_gloss(
                    document_id=document["id"],
                    remarks=remarks,
                    model_client=wrapper,
                    blob_store=self.blob_store,
                    claim_id=document["claim_id"],
                )
                output["_model"]["cost_usd"] += gloss_cost
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "EXTRACT", output),
            output=output,
        )

    def _cite(self, document: dict[str, Any]) -> StageResult:
        extracted = self._load_output(document["id"], "EXTRACT")
        classified = self._load_output(document["id"], "CLASSIFY")
        normalised = self._load_output(document["id"], "NORMALIZE")
        doc_type = self._document(document["id"])["doc_type"] or classified["doc_type"]
        schema = self.registry.schema_for(doc_type)
        wrapper = self._model_wrapper(document["id"])
        starting_cost = wrapper.spent_usd
        cited_fields = []
        for field in extracted["fields"]:
            candidate = dict(field)
            citation_mode = field.get("citation_mode", "anchor_text")
            candidate["citation_mode"] = citation_mode
            if citation_mode == "vision_bbox":
                page_index = field.get("page", 0) - 1
                bbox = normalized_bbox(field.get("bbox"))
                page_eligible = (
                    isinstance(page_index, int)
                    and 0 <= page_index < len(normalised["page_text_coverages"])
                    and eligible(
                        handwritten=schema.get("handwritten") is True,
                        text_coverage=float(normalised["page_text_coverages"][page_index]),
                        threshold=float(self.model_config["vision_text_coverage_threshold"]),
                    )
                )
                if bbox is None or not page_eligible:
                    candidate["citation"] = None
                    candidate["citation_failed"] = True
                    cited_fields.append(candidate)
                    continue
                crop_key = f"crops/{document['id']}/{field['name']}-{field['page']}.png"
                crop_bytes = crop_png(
                    self.blob_store.get(normalised["page_keys"][page_index]), bbox
                )
                self.blob_store.put(crop_key, crop_bytes)
                visible, _cost = verify_crop(
                    value=field["value"],
                    crop_png_bytes=crop_bytes,
                    model_client=wrapper,
                    claim_id=document["claim_id"],
                    document_id=document["id"],
                )
                candidate["citation"] = {
                    "bbox": bbox,
                    "vision_verified": visible,
                    "crop_key": crop_key,
                }
                candidate["citation_failed"] = not visible
                cited_fields.append(candidate)
                continue
            text_key = f"text/{document['id']}/{field['page']}.json"
            words = (
                json.loads(self.blob_store.get(text_key))
                if self.blob_store.exists(text_key)
                else []
            )
            match = match_anchor(field.get("anchor_text", ""), words)
            if match is None:
                candidate["citation"] = None
                candidate["citation_failed"] = True
            else:
                candidate["citation"] = {"bbox": match.bbox, "score": match.score}
                candidate["citation_failed"] = False
            cited_fields.append(candidate)
        output = {
            "fields": cited_fields,
            "_model": {"cost_usd": wrapper.spent_usd - starting_cost},
        }
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "CITE", output),
            output=output,
        )

    def _validate(self, document: dict[str, Any]) -> StageResult:
        cited = self._load_output(document["id"], "CITE")
        classified = self._load_output(document["id"], "CLASSIFY")
        doc_type = self._document(document["id"])["doc_type"] or classified["doc_type"]
        schema = self.registry.schema_for(doc_type)
        validated_fields = []
        for field in cited["fields"]:
            definition = schema["fields"][field["name"]]
            result = validate_field(
                definition["validator"],
                field["value"],
                today=self.clock().date(),
            )
            combined = (
                combined_confidence(field["confidence"], result.outcome)
                if not field["citation_failed"]
                else combined_confidence(0, result.outcome)
            )
            if field.get("citation_mode") == "vision_bbox":
                combined *= Decimal(str(self.model_config["vision_confidence_multiplier"]))
            candidate = dict(field)
            candidate.update(
                {
                    "normalised_value": result.value,
                    "validator_outcome": result.outcome,
                    "combined_confidence": float(combined),
                    "threshold": float(threshold_for(definition)),
                }
            )
            validated_fields.append(candidate)
        by_name = {field["name"]: field for field in validated_fields}
        line_items = by_name.get("line_items")
        total = by_name.get("total")
        if line_items is not None and total is not None and isinstance(
            line_items["value"], list
        ):
            parsed_amounts = [
                money_kes(line.get("amount"))
                for line in line_items["value"]
                if isinstance(line, dict)
            ]
            amounts_are_usable = len(parsed_amounts) == len(line_items["value"]) and all(
                amount.outcome == "pass" for amount in parsed_amounts
            )
            if amounts_are_usable:
                checked = sum_check(
                    [amount.value for amount in parsed_amounts],
                    total["normalised_value"],
                )
                if checked.outcome == "fail":
                    total["validator_outcome"] = "fail"
                    total["combined_confidence"] = float(
                        combined_confidence(
                            0 if total["citation_failed"] else total["confidence"],
                            "fail",
                        )
                    )
            else:
                total["validator_outcome"] = "out_of_scope"
                total["combined_confidence"] = float(
                    combined_confidence(
                        0 if total["citation_failed"] else total["confidence"],
                        "out_of_scope",
                    )
                )
        output = {"fields": validated_fields}
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "VALIDATE", output),
            output=output,
        )

    def _commit(self, document: dict[str, Any]) -> StageResult:
        validated = self._load_output(document["id"], "VALIDATE")
        classified = self._load_output(document["id"], "CLASSIFY")
        doc_type = self._document(document["id"])["doc_type"] or classified["doc_type"]
        schema = self.registry.schema_for(doc_type)
        writes, reviews = prepare_commit(
            document_id=document["id"],
            doc_type=doc_type,
            fields=validated["fields"],
            schema=schema,
            blob_store=self.blob_store,
            review_capability_id=self.review_capability_id,
        )
        committed_paths = commit_writes(
            service=self.claim_service,
            claim_id=document["claim_id"],
            document_id=document["id"],
            writes=writes,
        )
        for review in reviews:
            self._review_once(document["claim_id"], review)
        self._emit_once(
            claim_id=document["claim_id"],
            event_type="document.extracted",
            payload={
                "document_id": document["id"],
                "doc_type": doc_type,
                "committed_paths": committed_paths,
            },
            identity={"document_id": document["id"]},
            document_status="extracted",
        )
        output = {"committed_paths": committed_paths, "review_items": reviews}
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "COMMIT", output),
            output=output,
        )

    def _consistency(self, document: dict[str, Any]) -> StageResult:
        claim, current, _blocked = self.claim_service.hydrate_claim(
            document["claim_id"], "agent:doc_intel"
        )
        observations: dict[str, dict[str, Any]] = {
            "claim": {
                "reg": getattr(current.get("vehicle.reg"), "value", None),
                "insured_name": getattr(current.get("parties.insured.name"), "value", None),
                "loss_date": getattr(current.get("loss.date"), "value", None),
                "narrative": getattr(current.get("loss.narrative"), "value", None),
            }
        }
        for row in self.claim_service.documents(claim.id):
            stages = self._stage_rows(row.id)
            if "EXTRACT" not in stages or stages["EXTRACT"].status != "succeeded":
                continue
            extracted = self._load_output(row.id, "EXTRACT")
            values = {field["name"]: field.get("value") for field in extracted.get("fields", [])}
            if row.doc_type == "photo_damage" and "photo_damage" in observations:
                observations["photo_damage"].setdefault("descriptions", []).append(
                    values.get("damage_description")
                )
            elif row.doc_type:
                observations[row.doc_type] = values
                if row.doc_type == "photo_damage":
                    observations[row.doc_type]["descriptions"] = [
                        values.get("damage_description")
                    ]
        definitions = load_definitions()
        results = evaluate_observations(observations, definitions=definitions)
        model_cost = 0.0
        narrative = observations["claim"].get("narrative")
        photos = [
            value
            for value in observations.get("photo_damage", {}).get("descriptions", [])
            if isinstance(value, str)
        ]
        if isinstance(narrative, str) and photos:
            cc5 = evaluate_cc5(
                narrative=narrative,
                photo_descriptions=photos,
                model_client=self._model_wrapper(document["id"]),
                claim_id=document["claim_id"],
                document_id=document["id"],
            )
            model_cost += cc5.pop("cost_usd")
            cc5["evidence"] = {"narrative": narrative, "photo_descriptions": photos}
            results.append(cc5)
        stored = []
        for result in results:
            fingerprint = hashlib.sha256(
                json.dumps(result.get("evidence", {}), sort_keys=True, default=str).encode()
            ).hexdigest()
            review_payload = (
                {
                    "type": "CONSISTENCY_FLAG",
                    "subtype": result["check_id"],
                    "document_id": document["id"],
                    "status": result["status"],
                    "severity": result["severity"],
                }
                if result.get("review_required")
                else None
            )
            if self.claim_service.append_consistency_result(
                document["claim_id"],
                result,
                input_fingerprint=fingerprint,
                review_payload=review_payload,
            ):
                stored.append(result["check_id"])
        output = {"stored_checks": stored, "_model": {"cost_usd": model_cost}}
        return StageResult(
            status="succeeded" if results else "skipped",
            output_ref=self._store_output(document["id"], "CONSISTENCY", output),
            output=output,
        )
    def _run_stage(self, document: dict[str, Any], stage: str) -> StageResult:
        handlers = {
            "NORMALIZE": self._normalise,
            "CLASSIFY": self._classify,
            "SPLIT": self._split,
            "EXTRACT": self._extract,
            "CITE": self._cite,
            "VALIDATE": self._validate,
            "COMMIT": self._commit,
            "CONSISTENCY": self._consistency,
        }
        return handlers[stage](document)

    def process_stage(
        self, document_id: str, stage: str, *, schedule_next: bool = False
    ) -> dict[str, Any]:
        """Run exactly one idempotent stage for Celery and synchronous callers."""

        if stage not in STAGES:
            raise ValueError(f"unknown pipeline stage {stage!r}")
        document = self._document(document_id)
        self._ensure_stages(document_id)
        row = self._stage_rows(document_id)[stage]
        if row.status in TERMINAL_STAGE_STATUSES:
            if schedule_next:
                index = STAGES.index(stage)
                if index + 1 < len(STAGES):
                    if self.stage_scheduler is None:
                        raise RuntimeError("stage chaining requires a scheduler")
                    self.stage_scheduler.schedule(document_id, STAGES[index + 1])
            return {"stage": stage, "status": row.status, "output_ref": row.output_ref}
        if not self._begin_stage(document_id, stage):
            row = self._stage_rows(document_id)[stage]
            return {"stage": stage, "status": row.status, "output_ref": row.output_ref}
        try:
            result = self._run_stage(document, stage)
        except ModelBudgetExceeded as error:
            self._review_once(
                document["claim_id"],
                {
                    "type": "EXCEPTION",
                    "subtype": "budget_exceeded",
                    "document_id": document_id,
                },
            )
            self._fail_stage(document_id, stage, error)
            result = StageResult(status="failed", last_error=str(error))
        except ModelSchemaError as error:
            self._review_once(
                document["claim_id"],
                {
                    "type": "EXCEPTION",
                    "subtype": "model_schema_invalid",
                    "document_id": document_id,
                },
            )
            self._fail_stage(document_id, stage, error)
            result = StageResult(status="failed", last_error=str(error))
        except (ModelUnavailable, PipelinePause) as error:
            self._pause_stage(document_id, stage, error)
            result = StageResult(status="paused", last_error=str(error))
        except (HumanOverrideProtected, NormaliseError) as error:
            self._fail_stage(document_id, stage, error)
            result = StageResult(status="failed", last_error=str(error))
        except Exception as error:
            self._fail_stage(document_id, stage, error)
            if schedule_next:
                rows = self._stage_rows(document_id)
                self._record_sample(document_id, rows["NORMALIZE"].created_at)
            raise
        else:
            self._finish_stage(document_id, stage, result)
            if schedule_next and result.status in TERMINAL_STAGE_STATUSES:
                index = STAGES.index(stage)
                if index + 1 < len(STAGES):
                    if self.stage_scheduler is None:
                        raise RuntimeError("stage chaining requires a scheduler")
                    self.stage_scheduler.schedule(document_id, STAGES[index + 1])
        if schedule_next and (
            result.status in {"failed", "paused"}
            or (stage == STAGES[-1] and result.status in TERMINAL_STAGE_STATUSES)
        ):
            rows = self._stage_rows(document_id)
            self._record_sample(document_id, rows["NORMALIZE"].created_at)
        return {"stage": stage, "status": result.status, "output_ref": result.output_ref}

    def _record_sample(
        self,
        document_id: str,
        started_at: datetime,
    ) -> None:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        elapsed = self.clock() - started_at
        duration_ms = max(0, int(elapsed.total_seconds() * 1000))
        total_cost = self._model_cost(document_id)
        self.sentinel.record(
            document_id=document_id, duration_ms=duration_ms, cost_usd=total_cost
        )

    def apply_human_boundaries(
        self, parent_document_id: str, *, boundaries: list[dict[str, Any]], actor: str
    ) -> list[str]:
        if re.fullmatch(r"user:[0-9A-HJKMNP-TV-Z]{26}", actor) is None:
            raise ClaimCoreError(
                422, "VALUE_TYPE_MISMATCH", "Split resolution requires actor user:<ULID>"
            )
        parent = self._document(parent_document_id)
        self._ensure_stages(parent_document_id)
        if parent["page_count"] is None:
            raise ClaimCoreError(422, "INVALID_SPLIT_BOUNDARY", "Parent page count is unknown")
        parsed = validate_boundaries(boundaries, parent["page_count"])
        normalised_key = f"normalised/{parent_document_id}.pdf"
        source_bytes = (
            self.blob_store.get(normalised_key)
            if self.blob_store.exists(normalised_key)
            else self.blob_store.get(parent["s3_key"])
        )
        child_ids = []
        for start_page, end_page in parsed:
            child = self.claim_service.add_split_document(
                parent_document_id,
                start_page=start_page,
                end_page=end_page,
                content=pdf_subset(source_bytes, start_page, end_page),
                actor=actor,
            )
            child_ids.append(child.id)
            self._ensure_stages(child.id)
            normalise_row = self._stage_rows(child.id)["NORMALIZE"]
            if normalise_row.status != "succeeded":
                result = self._normalise(self._document(child.id))
                self._finish_stage(child.id, "NORMALIZE", result)
            if self.runtime_mode != "test":
                if self.stage_scheduler is None:
                    raise RuntimeError("operational split resolution requires a scheduler")
                self.stage_scheduler.schedule(child.id, "CLASSIFY")
        resolution = {
            "boundaries": [
                {"start_page": start_page, "end_page": end_page}
                for start_page, end_page in parsed
            ],
            "child_document_ids": child_ids,
            "resolved_by": actor,
        }
        self._finish_stage(
            parent_document_id,
            "SPLIT",
            StageResult(
                status="succeeded",
                output_ref=self._store_output(parent_document_id, "SPLIT", resolution),
                output=resolution,
            ),
        )
        for stage in ("EXTRACT", "CITE", "VALIDATE", "COMMIT", "CONSISTENCY"):
            self._finish_stage(
                parent_document_id,
                stage,
                StageResult(status="skipped", last_error="split parent; process children"),
            )
        return child_ids

    def process_document(self, document_id: str) -> PipelineOutcome:
        """Run or resume at the first non-terminal stage."""

        started_at = self.clock()
        document = self._document(document_id)
        self._ensure_stages(document_id)
        for stage in STAGES:
            result = self.process_stage(document_id, stage)
            if result["status"] not in TERMINAL_STAGE_STATUSES:
                break
            document = self._document(document_id)
        rows = self._stage_rows(document_id)
        commit_output: dict[str, Any] = {}
        if rows["COMMIT"].status == "succeeded":
            commit_output = self._load_output(document_id, "COMMIT")
        review_items = [
            dict(event.payload)
            for event in self.claim_service.timeline(document["claim_id"])
            if event.type == "review.created"
            and event.payload.get("document_id") == document_id
        ]
        outcome = PipelineOutcome(
            document_id=document_id,
            stages={stage: rows[stage].status for stage in STAGES},
            committed_paths=list(commit_output.get("committed_paths", [])),
            review_items=review_items,
            failed=any(row.status in {"failed", "paused"} for row in rows.values()),
        )
        self._record_sample(document_id, started_at)
        return outcome


def build_engine(
    app: FastAPI,
    *,
    model_client: ModelClient,
    ocr_engine: OcrEngine | None = None,
    clock: Callable[[], datetime] | None = None,
    model_config: Mapping[str, Any] | None = None,
    alert_sink: Any | None = None,
    runtime_mode: str = "test",
    stage_scheduler: Any | None = None,
) -> DocIntelEngine:
    """Build, expose, and register the doc-intel event consumer."""

    engine = DocIntelEngine(
        app,
        model_client=model_client,
        ocr_engine=ocr_engine,
        clock=clock,
        model_config=model_config,
        alert_sink=alert_sink,
        runtime_mode=runtime_mode,
        stage_scheduler=stage_scheduler,
    )
    app.state.doc_intel = engine
    app.state.dispatcher.register_consumer("doc_intel", engine.consume)
    return engine
