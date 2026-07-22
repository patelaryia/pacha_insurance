"""Approval-pack orchestration: selection, generation, and the two AR-2 gates.

Merge and note each cross their own capability gate. Nothing in this module
sends, pays, or approves; the only external effects are immutable artifact
writes and appended events.
"""

from __future__ import annotations

import json
from threading import Lock
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from agent_runtime import Action
from approval_pack_agent.config import ApprovalPackConfig, utc_rfc3339
from approval_pack_agent.conversion import (
    ConversionFailed,
    LocalImmutableStore,
    is_pdf,
    page_count,
    sha256_hex,
)
from approval_pack_agent.merge import MergeEngine
from approval_pack_agent.note import CommentaryInvalid, NoteInputsInvalid, NoteService
from approval_pack_agent.resolver import ACTOR, ReadinessEngine, _json
from claim_core import ClaimCoreError, new_ulid

OPERATIONAL_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "head_of_claims",
        "gm",
        "md",
        "chairman",
    }
)
UPLOAD_ITEM_IDS = frozenset({"assessor_payment_request", "claim_details_report"})
SELECTABLE_KINDS = frozenset({"document", "communication"})


class ApprovalPackService:
    """The curated public service exposed as ``app.state.approval_pack_agent``."""

    def __init__(
        self,
        app: Any,
        config: ApprovalPackConfig,
        *,
        model_client: Any,
        html_renderer: Any,
        immutable_store: Any | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.store = immutable_store or LocalImmutableStore(app.state.blob_store)
        self.readiness = ReadinessEngine(app, config)
        self.merge = MergeEngine(app, config, renderer=html_renderer, store=self.store)
        self.notes = NoteService(app, config, model_client=model_client, store=self.store)
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self._version_lock = Lock()

    # -- shared helpers --------------------------------------------------------

    def require_role(self, actor: str) -> str:
        role = self.app.state.review_queue.service.authorizer.role(actor)
        if role not in OPERATIONAL_ROLES:
            raise ClaimCoreError(
                403, "FORBIDDEN_ROLE", "Role cannot manage the approval pack"
            )
        return str(role)

    def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(text(sql), params).mappings()
            return [dict(row) for row in rows]

    def _events(self, claim_id: str, event_type: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT id, correlation_id, payload FROM events WHERE claim_id = :claim_id "
            "AND type = :event_type ORDER BY seq",
            claim_id=claim_id,
            event_type=event_type,
        )
        return [{**row, "payload": _json(row["payload"])} for row in rows]

    def _emit(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str | None,
        actor: str = ACTOR,
    ) -> str:
        with self.sessions.begin() as session:
            event = self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor=actor,
                correlation_id=correlation_id,
            )
            return event.id

    def _claim_exists(self, claim_id: str) -> None:
        rows = self._rows("SELECT id FROM claims WHERE id = :claim_id", claim_id=claim_id)
        if not rows:
            raise ClaimCoreError(404, "CLAIM_NOT_FOUND", "Claim was not found")

    def _review_items(self, claim_id: str, type_name: str) -> list[dict[str, Any]]:
        self.app.state.review_queue.backfill(ACTOR)
        rows = self._rows(
            "SELECT id, type, subtype, status, payload FROM review_items "
            "WHERE claim_id = :claim_id AND type = :type ORDER BY created_at, id",
            claim_id=claim_id,
            type=type_name,
        )
        return [{**row, "payload": _json(row["payload"])} for row in rows]

    # -- source selection ------------------------------------------------------

    def select_sources(
        self, claim_id: str, item_id: str, sources: list[dict[str, str]], actor: str
    ) -> dict[str, Any]:
        """Append one complete ordered selection; never update a prior event."""

        self.require_role(actor)
        self._claim_exists(claim_id)
        try:
            item = self.config.item(item_id)
        except LookupError as error:
            raise ClaimCoreError(404, "ITEM_NOT_FOUND", "Manifest item was not found") from error
        allowed = set(item.source_kinds) & SELECTABLE_KINDS
        if not allowed:
            raise ClaimCoreError(
                422,
                "SOURCE_KIND_NOT_ALLOWED",
                "This manifest item is resolved by projection or upload",
            )
        references: list[dict[str, str]] = []
        for raw in sources:
            kind = raw.get("kind")
            source_id = raw.get("id")
            if kind not in allowed or not isinstance(source_id, str) or not source_id:
                raise ClaimCoreError(
                    422, "SOURCE_KIND_NOT_ALLOWED", "Source kind is not allowed for this item"
                )
            references.append({"kind": kind, "id": source_id})
        if not references or (not item.repeatable and len(references) != 1):
            raise ClaimCoreError(
                422, "SOURCE_CARDINALITY_INVALID", "Selection cardinality is invalid"
            )
        if len({(row["kind"], row["id"]) for row in references}) != len(references):
            raise ClaimCoreError(
                422, "SOURCE_CARDINALITY_INVALID", "Selection repeats a source"
            )
        for reference in references:
            table = "documents" if reference["kind"] == "document" else "communications"
            owned = self._rows(
                f"SELECT id FROM {table} WHERE id = :id AND claim_id = :claim_id",  # noqa: S608
                id=reference["id"],
                claim_id=claim_id,
            )
            if not owned:
                # A source belonging to another claim is indistinguishable from absent.
                raise ClaimCoreError(404, "SOURCE_NOT_FOUND", "Source was not found")
        events = [
            event
            for event in self._events(claim_id, "pack.sources_selected")
            if event["payload"].get("item_id") == item_id
        ]
        prior = events[-1] if events else None
        if prior is not None and prior["payload"].get("sources") == references:
            return {"item_id": item_id, "sources": references, "recorded": False}
        self._emit(
            claim_id=claim_id,
            event_type="pack.sources_selected",
            payload={
                "item_id": item_id,
                "sources": references,
                "actor": actor,
                "prior_event_id": None if prior is None else prior["id"],
            },
            correlation_id=None,
            actor=actor,
        )
        return {"item_id": item_id, "sources": references, "recorded": True}

    def upload_item(
        self, claim_id: str, item_id: str, *, filename: str, mime: str, content: bytes, actor: str
    ) -> dict[str, Any]:
        """Accept an officer PDF for the two projection-backed manifest rows."""

        self.require_role(actor)
        self._claim_exists(claim_id)
        if item_id not in UPLOAD_ITEM_IDS:
            raise ClaimCoreError(
                404, "ITEM_NOT_FOUND", "This manifest item does not accept uploads"
            )
        if not is_pdf(content):
            raise ClaimCoreError(422, "INVALID_PDF", "Upload must be a PDF")
        try:
            page_count(content)
        except ConversionFailed as error:
            raise ClaimCoreError(422, "INVALID_PDF", "Upload is not a parseable PDF") from error
        digest = sha256_hex(content)
        upload_id = new_ulid()
        blob_key = f"approval-packs/{claim_id}/uploads/{item_id}/{digest}.pdf"
        self.store.put_immutable(blob_key, content, retention=self.config.retention)
        received_at = utc_rfc3339(self.app.state.clock())
        self._emit(
            claim_id=claim_id,
            event_type="pack.item_uploaded",
            payload={
                "item_id": item_id,
                "upload_id": upload_id,
                "blob_key": blob_key,
                "filename": filename,
                "mime": mime,
                "sha256": digest,
                "received_at": received_at,
            },
            correlation_id=None,
            actor=actor,
        )
        return {
            "item_id": item_id,
            "upload_id": upload_id,
            "sha256": digest,
            "received_at": received_at,
        }

    # -- generation ------------------------------------------------------------

    def _requested(self, claim_id: str, key: str) -> dict[str, Any] | None:
        for event in self._events(claim_id, "pack.merge_requested"):
            if event["payload"].get("idempotency_key") == key:
                return event
        return None

    def _staged_release(
        self, claim_id: str, request_event_id: str, capability_id: str
    ) -> str | None:
        for item in self._review_items(claim_id, "DRAFT_RELEASE"):
            payload = item["payload"]
            action = payload.get("action")
            if payload.get("capability_id") != capability_id or not isinstance(action, dict):
                continue
            if action.get("payload", {}).get("request_event_id") == request_event_id:
                return item["id"]
        return None

    def _merged_event(self, claim_id: str, request_event_id: str) -> dict[str, Any] | None:
        for event in self._events(claim_id, "pack.merged"):
            if event["correlation_id"] == request_event_id:
                return event
        return None

    def _note_review_item(self, claim_id: str, draft_id: str) -> str | None:
        for item in self._review_items(claim_id, "NOTE_REVIEW"):
            if item["payload"].get("note_draft_id") == draft_id:
                return item["id"]
        return None

    def _draft_for(self, claim_id: str, pack_event_id: str) -> dict[str, Any] | None:
        for row in self._rows(
            "SELECT id, version, status, body FROM note_drafts WHERE claim_id = :claim_id "
            "ORDER BY version",
            claim_id=claim_id,
        ):
            body = _json(row["body"])
            if isinstance(body, dict) and body.get("merged_pack", {}).get(
                "event_id"
            ) == pack_event_id:
                return {**row, "body": body}
        return None

    def _response(self, claim_id: str, request_event_id: str) -> tuple[int, dict[str, Any]]:
        merged = self._merged_event(claim_id, request_event_id)
        if merged is None:
            review_id = self._staged_release(claim_id, request_event_id, "pack.merge")
            return 202, {
                "status": "staged",
                "capability_id": "pack.merge",
                "review_item_id": review_id,
            }
        payload = merged["payload"]
        body: dict[str, Any] = {
            "status": "merged",
            "pack_version": payload["version"],
            "pack_event_id": merged["id"],
        }
        draft = self._draft_for(claim_id, merged["id"])
        if draft is not None and draft["status"] in {"in_review", "superseded"}:
            review_id = self._note_review_item(claim_id, draft["id"])
            body.update(
                {
                    "status": "ready_for_note_review",
                    "note_status": "in_review",
                    "note_draft_id": draft["id"],
                    "note_version": draft["version"],
                    "note_review_item_id": review_id,
                }
            )
            return 201, body
        if draft is not None:
            body["note_status"] = "blocked_on_integrity"
            body["note_draft_id"] = draft["id"]
            return 201, body
        staged_note = self._staged_release(claim_id, request_event_id, "pack.note_draft")
        if staged_note is not None:
            body["note_status"] = "staged"
            body["note_review_item_id"] = staged_note
            return 201, body
        body["note_status"] = "blocked_on_exception"
        return 201, body

    def generate(
        self, claim_id: str, *, actor: str, idempotency_key: str, fingerprint: str
    ) -> tuple[int, dict[str, Any]]:
        """Request one immutable pack version under the readiness fingerprint."""

        self.require_role(actor)
        self._claim_exists(claim_id)
        if not idempotency_key.strip():
            raise ClaimCoreError(
                422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key must be non-empty"
            )
        existing = self._requested(claim_id, idempotency_key)
        if existing is not None:
            refused = [
                event
                for event in self._events(claim_id, "pack.generation_refused")
                if event["correlation_id"] == existing["id"]
            ]
            if refused:
                readiness = self.readiness.evaluate(claim_id, actor)
                raise ClaimCoreError(
                    409,
                    str(refused[-1]["payload"].get("code", "PACK_NOT_READY")),
                    "Approval pack generation was refused",
                    extra={"readiness": readiness.card()},
                )
            return self._response(claim_id, existing["id"])

        readiness = self.readiness.evaluate(claim_id, actor)
        request_event_id = self._emit(
            claim_id=claim_id,
            event_type="pack.merge_requested",
            payload={
                "idempotency_key": idempotency_key,
                "readiness_fingerprint": fingerprint,
                "actor": actor,
            },
            correlation_id=None,
            actor=actor,
        )
        code = None
        if not readiness.ready:
            code = "PACK_NOT_READY"
        elif readiness.fingerprint != fingerprint:
            code = "READINESS_STALE"
        if code is not None:
            self._emit(
                claim_id=claim_id,
                event_type="pack.generation_refused",
                payload={
                    "code": code,
                    "idempotency_key": idempotency_key,
                    "blockers": readiness.blockers,
                },
                correlation_id=request_event_id,
                actor=actor,
            )
            raise ClaimCoreError(
                409,
                code,
                "Approval pack generation was refused",
                extra={"readiness": readiness.card()},
            )
        self.app.state.agent_runtime.execute_or_stage(
            capability_id="pack.merge",
            action=Action(
                type="pack.merge",
                payload={
                    "claim_id": claim_id,
                    "request_event_id": request_event_id,
                    "readiness_fingerprint": fingerprint,
                    "actor": actor,
                },
            ),
            claim_id=claim_id,
            actor=ACTOR,
        )
        return self._response(claim_id, request_event_id)

    # -- executors -------------------------------------------------------------

    def _next_pack_version(self, claim_id: str) -> int:
        highest = 0
        for event in self._events(claim_id, "pack.merged"):
            candidate = event["payload"].get("version")
            if isinstance(candidate, int) and candidate > highest:
                highest = candidate
        return highest + 1

    def execute_merge(self, action: Action) -> None:
        """Build, store, and index exactly one immutable pack version."""

        payload = action.payload
        claim_id = str(payload["claim_id"])
        request_event_id = str(payload["request_event_id"])
        actor = str(payload["actor"])
        if self._merged_event(claim_id, request_event_id) is not None:
            return
        readiness = self.readiness.evaluate(claim_id, actor)
        if not readiness.ready or readiness.fingerprint != payload["readiness_fingerprint"]:
            self._emit(
                claim_id=claim_id,
                event_type="pack.generation_refused",
                payload={
                    "code": "READINESS_STALE",
                    "idempotency_key": None,
                    "blockers": readiness.blockers,
                },
                correlation_id=request_event_id,
                actor=actor,
            )
            return
        with self._version_lock:
            with self.sessions.begin() as session:
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT id FROM claims WHERE id = :claim_id FOR UPDATE"),
                        {"claim_id": claim_id},
                    )
                version = self._next_pack_version(claim_id)
            try:
                merged = self.merge.build(
                    claim_id=claim_id,
                    readiness=readiness,
                    version=version,
                    rendered_at=self.app.state.clock(),
                )
            except ConversionFailed as error:
                self.notes.refuse(
                    claim_id,
                    subtype="pack_conversion_failed",
                    facts={"code": error.code, "detail": error.detail},
                    risk="the approval pack cannot be assembled from the resolved sources",
                    recommendation="repair or reselect the named source and regenerate",
                    correlation_id=request_event_id,
                )
                return
            merged_event_id = self._emit(
                claim_id=claim_id,
                event_type="pack.merged",
                payload=merged,
                correlation_id=request_event_id,
                actor=actor,
            )
        self.app.state.eval_harness.grade(
            "G-TPL",
            {
                "claim_id": claim_id,
                "template_id": "PACK-MERGED",
                "artifact_kind": "merged_pack",
                "blob_key": merged["blob_key"],
                "sha256": merged["sha256"],
                "manifest": merged["manifest"],
                "manifest_version": merged["manifest_version"],
                "source_event_id": merged_event_id,
                "capability_id": "pack.merge",
            },
            actor="agent:eval",
        )
        self.app.state.agent_runtime.execute_or_stage(
            capability_id="pack.note_draft",
            action=Action(
                type="pack.note_draft",
                payload={
                    "claim_id": claim_id,
                    "request_event_id": request_event_id,
                    "merged_event_id": merged_event_id,
                    "actor": actor,
                },
            ),
            claim_id=claim_id,
            actor=ACTOR,
        )

    def execute_note_draft(self, action: Action) -> None:
        """Generate, grade, persist, and hand off exactly one note version."""

        payload = action.payload
        claim_id = str(payload["claim_id"])
        merged_event_id = str(payload["merged_event_id"])
        actor = str(payload["actor"])
        if self._draft_for(claim_id, merged_event_id) is not None:
            return
        merged = next(
            (
                event
                for event in self._events(claim_id, "pack.merged")
                if event["id"] == merged_event_id
            ),
            None,
        )
        if merged is None:
            return
        readiness = self.readiness.evaluate(claim_id, actor)
        version = self.notes._next_version(claim_id)
        try:
            candidate = self.notes.build_candidate(
                claim_id=claim_id,
                actor=actor,
                readiness=readiness,
                merged_event_id=merged_event_id,
                merged_payload=merged["payload"],
                version=version,
            )
        except CommentaryInvalid as error:
            self.notes.refuse(
                claim_id,
                subtype="note_commentary_invalid",
                facts={"validation_errors": error.errors, "note_version": version},
                risk="unverifiable approval-note prose cannot reach a human signer",
                recommendation="review the cited inputs and regenerate the approval pack",
                correlation_id=merged_event_id,
            )
            return
        except NoteInputsInvalid as error:
            self.notes.refuse(
                claim_id,
                subtype="note_inputs_invalid",
                facts={"detail": str(error), "note_version": version},
                risk="a rendered figure would lack resolved provenance",
                recommendation="verify and cite the named field before regenerating",
                correlation_id=merged_event_id,
            )
            return
        integrity = self.notes.grade(claim_id, candidate)
        body = {**candidate.body, "integrity": integrity}
        if integrity["g_tpl_result"] != "pass" or integrity["g_note_result"] != "pass":
            draft_id = self.notes.persist(
                claim_id, version=version, body=body, status="draft"
            )
            self.notes.refuse(
                claim_id,
                subtype="note_integrity_failed",
                facts={
                    "note_draft_id": draft_id,
                    "note_version": version,
                    "integrity": integrity,
                },
                risk="a known-wrong approval note must never enter human signing",
                recommendation="inspect the failed grader run and regenerate the note",
                correlation_id=merged_event_id,
            )
            return
        draft_id = self.notes.persist(
            claim_id, version=version, body=body, status="in_review"
        )
        self.notes.hand_off(
            claim_id,
            draft_id=draft_id,
            version=version,
            body=body,
            candidate=candidate,
            merged_payload=merged["payload"],
            merged_event_id=merged_event_id,
            actor=actor,
        )

    # -- consumer --------------------------------------------------------------

    def consume(self, event: Any) -> None:
        """Execute a released pack action after its human DRAFT_RELEASE approval."""

        if event.type != "review.resolved":
            return
        payload = event.payload if isinstance(event.payload, dict) else json.loads(event.payload)
        if payload.get("type") != "DRAFT_RELEASE" or payload.get("resolution") != "approved":
            return
        review_id = payload.get("review_id")
        if not isinstance(review_id, str):
            return
        rows = self._rows(
            "SELECT payload FROM review_items WHERE id = :review_id", review_id=review_id
        )
        if not rows:
            return
        item_payload = _json(rows[0]["payload"])
        action = item_payload.get("action") if isinstance(item_payload, dict) else None
        if not isinstance(action, dict) or action.get("type") not in {
            "pack.merge",
            "pack.note_draft",
        }:
            return
        self.app.state.agent_runtime.execute_staged(
            Action(type=action["type"], payload=dict(action["payload"]))
        )

    # -- read surface for PACKET-19 -------------------------------------------

    def versions(self, claim_id: str, actor: str) -> list[dict[str, Any]]:
        """Return the append-only merged-version index for one claim."""

        self.require_role(actor)
        return [
            {
                "event_id": event["id"],
                "version": event["payload"]["version"],
                "filename": event["payload"]["filename"],
                "blob_key": event["payload"]["blob_key"],
                "sha256": event["payload"]["sha256"],
                "rendered_at": event["payload"]["rendered_at"],
                "object_lock_status": event["payload"]["object_lock_status"],
            }
            for event in self._events(claim_id, "pack.merged")
        ]

    def note_drafts(self, claim_id: str, actor: str) -> list[dict[str, Any]]:
        """Return every retained note version for one claim."""

        self.require_role(actor)
        return [
            {
                "id": row["id"],
                "version": row["version"],
                "status": row["status"],
                "body": _json(row["body"]),
            }
            for row in self._rows(
                "SELECT id, version, status, body FROM note_drafts "
                "WHERE claim_id = :claim_id ORDER BY version",
                claim_id=claim_id,
            )
        ]


__all__ = ["ApprovalPackService"]
