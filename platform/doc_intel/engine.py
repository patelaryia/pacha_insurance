"""Document-intelligence pipeline orchestration and application wiring."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from claim_core import (
    Base,
    BlobStore,
    ClaimService,
    HumanOverrideProtected,
    field_dictionary,
    new_ulid,
    register_dictionary_extensions,
)
from doc_intel.citations import match_anchor
from doc_intel.commit import commit_writes, prepare_commit
from doc_intel.confidence import combined_confidence, threshold_for
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
from doc_intel.stages import (
    STAGES,
    TERMINAL_STAGE_STATUSES,
    DocumentStage,
    PipelineOutcome,
    StageResult,
)
from doc_intel.validators import money_kes, sum_check, validate_field

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
    ) -> None:
        self.app = app
        self.engine: Engine = app.state.engine
        self.claim_service: ClaimService = app.state.claim_service
        self.blob_store: BlobStore = app.state.blob_store
        self.clock = clock or utc_now
        self.ocr_engine = ocr_engine or TesseractOcrEngine()
        self.model_client = model_client
        self.model_config = dict(model_config or {})
        root = Path(__file__).resolve().parents[2]
        register_dictionary_extensions(root / "packs" / "motor" / "fields.yaml")
        self.registry = SchemaRegistry(field_dictionary())
        self.registry.load_directory(Path(__file__).with_name("schemas") / "motor")
        Base.metadata.create_all(self.engine, tables=[DocumentStage.__table__])

    def consume(self, event: Any) -> None:
        """Transactional-outbox consumer entry; unrelated event types are no-ops."""

        if event.type != "document.received":
            return
        document_id = event.payload.get("document_id")
        if isinstance(document_id, str):
            self.process_document(document_id)

    def _document(self, document_id: str) -> dict[str, Any]:
        row = self.claim_service.get_document(document_id)
        return {
            "id": row.id,
            "claim_id": row.claim_id,
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

    def _begin_stage(self, document_id: str, stage: str) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(DocumentStage).where(
                    DocumentStage.document_id == document_id,
                    DocumentStage.stage == stage,
                )
            )
            if row is None:
                raise RuntimeError(f"missing durable stage {stage}")
            row.attempts += 1
            row.last_error = None
            row.updated_at = self.clock()

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
        rows = self._stage_rows(document_id)
        for stage in ("CLASSIFY", "EXTRACT"):
            row = rows[stage]
            if row.status != "succeeded" or row.output_ref is None:
                continue
            output = json.loads(self.blob_store.get(row.output_ref))
            wrapper.spent_usd += float(output.get("_model", {}).get("cost_usd", 0))
        return wrapper

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
                "filename": document["filename"],
                "first_text": normalised["first_text"],
                "page_1_key": normalised["page_keys"][0],
                "source": document["source"],
            },
        )
        output = {
            **result["data"],
            "_model": {"cost_usd": result["cost_usd"], "model_id": result["model_id"]},
        }
        doc_type = output["doc_type"]
        confidence = output["confidence"]
        if (
            doc_type == "other"
            or doc_type not in self.registry.doc_types()
            or confidence < float(DEFAULTS["classification_confidence_threshold"])
        ):
            self._review_once(
                document["claim_id"],
                {
                    "type": "DOC_CLASSIFY",
                    "document_id": document["id"],
                    "candidate_doc_type": doc_type,
                    "confidence": confidence,
                },
            )
            raise PipelinePause("classification requires human review")
        self.claim_service.set_document_status(
            document["id"], doc_type=doc_type, status="classified"
        )
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "CLASSIFY", output),
            output=output,
        )

    def _extract(self, document: dict[str, Any]) -> StageResult:
        classified = self._load_output(document["id"], "CLASSIFY")
        normalised = self._load_output(document["id"], "NORMALIZE")
        doc_type = classified["doc_type"]
        wrapper = self._model_wrapper(document["id"])
        result = wrapper.structured_call(
            tier="MODEL_HEAVY",
            schema=self.registry.extraction_output_schema(doc_type),
            inputs={
                "prompt": self.registry.prompt_for(doc_type),
                "text_keys": normalised["text_keys"],
                "page_keys": normalised["page_keys"],
            },
        )
        output = {
            **result["data"],
            "_model": {"cost_usd": result["cost_usd"], "model_id": result["model_id"]},
        }
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "EXTRACT", output),
            output=output,
        )

    def _cite(self, document: dict[str, Any]) -> StageResult:
        extracted = self._load_output(document["id"], "EXTRACT")
        cited_fields = []
        for field in extracted["fields"]:
            candidate = dict(field)
            text_key = f"text/{document['id']}/{field['page']}.json"
            words = (
                json.loads(self.blob_store.get(text_key))
                if self.blob_store.exists(text_key)
                else []
            )
            match = match_anchor(field["anchor_text"], words)
            if match is None:
                candidate["citation"] = None
                candidate["citation_failed"] = True
            else:
                candidate["citation"] = {"bbox": match.bbox, "score": match.score}
                candidate["citation_failed"] = False
            cited_fields.append(candidate)
        output = {"fields": cited_fields}
        return StageResult(
            status="succeeded",
            output_ref=self._store_output(document["id"], "CITE", output),
            output=output,
        )

    def _validate(self, document: dict[str, Any]) -> StageResult:
        cited = self._load_output(document["id"], "CITE")
        classified = self._load_output(document["id"], "CLASSIFY")
        schema = self.registry.schema_for(classified["doc_type"])
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
        schema = self.registry.schema_for(classified["doc_type"])
        writes, reviews = prepare_commit(
            document_id=document["id"],
            doc_type=classified["doc_type"],
            fields=validated["fields"],
            schema=schema,
            blob_store=self.blob_store,
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
                "doc_type": classified["doc_type"],
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

    def _run_stage(self, document: dict[str, Any], stage: str) -> StageResult:
        if stage in {"SPLIT", "CONSISTENCY"}:
            return StageResult(status="skipped", last_error="packet-05")
        handlers = {
            "NORMALIZE": self._normalise,
            "CLASSIFY": self._classify,
            "EXTRACT": self._extract,
            "CITE": self._cite,
            "VALIDATE": self._validate,
            "COMMIT": self._commit,
        }
        return handlers[stage](document)

    def process_document(self, document_id: str) -> PipelineOutcome:
        """Run or resume at the first non-terminal stage."""

        document = self._document(document_id)
        self._ensure_stages(document_id)
        for stage in STAGES:
            row = self._stage_rows(document_id)[stage]
            if row.status in TERMINAL_STAGE_STATUSES:
                continue
            self._begin_stage(document_id, stage)
            try:
                result = self._run_stage(document, stage)
            except ModelBudgetExceeded as error:
                review = {
                    "type": "EXCEPTION",
                    "subtype": "budget_exceeded",
                    "document_id": document_id,
                }
                self._review_once(document["claim_id"], review)
                self._fail_stage(document_id, stage, error)
                break
            except ModelSchemaError as error:
                review = {
                    "type": "EXCEPTION",
                    "subtype": "model_schema_invalid",
                    "document_id": document_id,
                }
                self._review_once(document["claim_id"], review)
                self._fail_stage(document_id, stage, error)
                break
            except (
                HumanOverrideProtected,
                ModelUnavailable,
                NormaliseError,
                PipelinePause,
            ) as error:
                self._fail_stage(document_id, stage, error)
                break
            except Exception as error:
                self._fail_stage(document_id, stage, error)
                raise
            else:
                self._finish_stage(document_id, stage, result)
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
        return PipelineOutcome(
            document_id=document_id,
            stages={stage: rows[stage].status for stage in STAGES},
            committed_paths=list(commit_output.get("committed_paths", [])),
            review_items=review_items,
            failed=any(row.status == "failed" for row in rows.values()),
        )


def build_engine(
    app: FastAPI,
    *,
    model_client: ModelClient,
    ocr_engine: OcrEngine | None = None,
    clock: Callable[[], datetime] | None = None,
    model_config: Mapping[str, Any] | None = None,
) -> DocIntelEngine:
    """Build, expose, and register the doc-intel event consumer."""

    engine = DocIntelEngine(
        app,
        model_client=model_client,
        ocr_engine=ocr_engine,
        clock=clock,
        model_config=model_config,
    )
    app.state.doc_intel = engine
    app.state.dispatcher.register_consumer("doc_intel", engine.consume)
    return engine
