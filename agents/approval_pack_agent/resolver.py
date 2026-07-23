"""Source resolution and the fail-closed readiness engine (PRD-08 §8.2/§8.3).

Nothing here guesses. Zero candidates is ``missing``, more than one candidate
for a non-repeatable row is ``ambiguous`` with the candidate ids reported, and
an unreadable or wrong-format source is ``invalid``. The readiness read never
writes a field, converts a source, calls a model, or emits an event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from approval_pack_agent.config import (
    ApprovalPackConfig,
    ManifestItem,
    canonical_json,
    utc_rfc3339,
)
from approval_pack_agent.conversion import is_pdf, page_count, sha256_hex

ACTOR = "agent:approval_pack"
READY_STATES = ("RESERVED", "PACK_READY")
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})
VERIFICATION_FLOOR = "human_verified"
BLOCKING_EXCEPTION_SUBTYPES = frozenset(
    {
        "pack_source_ambiguous",
        "pack_integrity_failed",
        "budget_exceeded",
        "assessment_report_revision_ambiguous",
        "note_integrity_failed",
        "note_commentary_invalid",
        "pack_conversion_failed",
    }
)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _aware(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ResolvedSource:
    """One concrete, readable source bound to a manifest row."""

    kind: str
    id: str
    filename: str
    received_at: datetime
    sha256: str
    blob_key: str
    mime: str
    doc_type: str | None = None
    page_count: int | None = None

    def card(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "filename": self.filename,
            "received_at": utc_rfc3339(self.received_at),
            "sha256": self.sha256,
        }


@dataclass
class ItemResolution:
    """The resolved state of one manifest row."""

    item: ManifestItem
    state: str
    sources: list[ResolvedSource] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)

    def card(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "order": self.item.order,
            "label": self.item.label,
            "state": self.state,
            "required": self.item.required,
            "waivable": self.item.waivable,
            "sources": [source.card() for source in self.sources],
            "blockers": list(self.blockers),
        }


@dataclass
class Readiness:
    """The complete readiness card and its time-of-check fingerprint."""

    claim_id: str
    status: str
    ready: bool
    fingerprint: str
    checklists: dict[str, Any]
    fields: dict[str, Any]
    items: list[ItemResolution]
    blockers: list[dict[str, Any]]
    field_rows: dict[str, Any] = field(default_factory=dict)

    def card(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "status": self.status,
            "ready": self.ready,
            "fingerprint": self.fingerprint,
            "checklists": self.checklists,
            "fields": self.fields,
            "items": [item.card() for item in self.items],
            "blockers": list(self.blockers),
        }

    def resolution(self, item_id: str) -> ItemResolution:
        for item in self.items:
            if item.item.id == item_id:
                return item
        raise LookupError(f"unknown manifest item {item_id!r}")


class SourceCatalog:
    """One snapshot of every durable input the manifest may resolve from."""

    def __init__(self, app: Any, claim_id: str) -> None:
        self.app = app
        self.claim_id = claim_id
        self.documents = self._documents()
        self.communications = self._communications()
        self.selection_events = self._events("pack.sources_selected")
        self.upload_events = self._events("pack.item_uploaded")
        self.report_events = self._events("assessment.report_received")
        self.selected_reports = self._events("assessment.selection_completed")

    def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(text(sql), params).mappings()
            return [dict(row) for row in rows]

    def _documents(self) -> dict[str, dict[str, Any]]:
        rows = self._rows(
            "SELECT id, doc_type, status, filename, mime, s3_key, sha256, page_count, "
            "received_at FROM documents WHERE claim_id = :claim_id "
            "ORDER BY received_at, id",
            claim_id=self.claim_id,
        )
        return {row["id"]: row for row in rows}

    def _communications(self) -> dict[str, dict[str, Any]]:
        rows = self._rows(
            "SELECT id, subject, from_addr, to_addrs, body_s3_key, occurred_at "
            "FROM communications WHERE claim_id = :claim_id ORDER BY occurred_at, id",
            claim_id=self.claim_id,
        )
        return {row["id"]: row for row in rows}

    def _events(self, event_type: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT id, payload FROM events WHERE claim_id = :claim_id "
            "AND type = :event_type ORDER BY seq",
            claim_id=self.claim_id,
            event_type=event_type,
        )
        return [{**row, "payload": _json(row["payload"])} for row in rows]

    def latest_selection(self, item_id: str) -> list[dict[str, str]] | None:
        chosen: list[dict[str, str]] | None = None
        for event in self.selection_events:
            payload = event["payload"]
            if payload.get("item_id") == item_id and isinstance(payload.get("sources"), list):
                chosen = [dict(source) for source in payload["sources"]]
        return chosen

    def latest_upload(self, item_id: str) -> dict[str, Any] | None:
        chosen: dict[str, Any] | None = None
        for event in self.upload_events:
            payload = event["payload"]
            if payload.get("item_id") == item_id:
                chosen = dict(payload)
        return chosen


class ManifestResolver:
    """Resolve every manifest row from the catalog, refusing on ambiguity."""

    def __init__(self, app: Any, config: ApprovalPackConfig) -> None:
        self.app = app
        self.config = config

    # -- source construction ---------------------------------------------------

    def _document_source(self, row: dict[str, Any]) -> tuple[ResolvedSource | None, str | None]:
        if row["status"] == "rejected":
            return None, "rejected_source"
        try:
            content = self.app.state.blob_store.get(row["s3_key"])
        except Exception:  # noqa: BLE001 - a missing blob is a visible blocker
            return None, "missing_blob"
        if sha256_hex(content) != row["sha256"]:
            return None, "digest_mismatch"
        return (
            ResolvedSource(
                kind="document",
                id=row["id"],
                filename=row["filename"],
                received_at=_aware(row["received_at"]),
                sha256=row["sha256"],
                blob_key=row["s3_key"],
                mime=row["mime"],
                doc_type=row["doc_type"],
                page_count=row["page_count"],
            ),
            None,
        )

    def _communication_source(
        self, row: dict[str, Any]
    ) -> tuple[ResolvedSource | None, str | None]:
        try:
            content = self.app.state.blob_store.get(row["body_s3_key"])
        except Exception:  # noqa: BLE001 - a missing archive is a visible blocker
            return None, "missing_blob"
        if not content.strip():
            return None, "invalid_archive"
        subject = row["subject"] or row["id"]
        return (
            ResolvedSource(
                kind="communication",
                id=row["id"],
                filename=f"{subject}.pdf",
                received_at=_aware(row["occurred_at"]),
                sha256=sha256_hex(content),
                blob_key=row["body_s3_key"],
                mime="text/html",
            ),
            None,
        )

    def _upload_source(self, payload: dict[str, Any]) -> tuple[ResolvedSource | None, str | None]:
        blob_key = payload.get("blob_key")
        if not isinstance(blob_key, str):
            return None, "missing_blob"
        try:
            content = self.app.state.blob_store.get(blob_key)
        except Exception:  # noqa: BLE001 - a missing blob is a visible blocker
            return None, "missing_blob"
        if sha256_hex(content) != payload.get("sha256"):
            return None, "digest_mismatch"
        return (
            ResolvedSource(
                kind="upload",
                id=str(payload["upload_id"]),
                filename=str(payload.get("filename", "upload.pdf")),
                received_at=_aware(payload["received_at"]),
                sha256=str(payload["sha256"]),
                blob_key=blob_key,
                mime=str(payload.get("mime", "application/pdf")),
                page_count=None,
            ),
            None,
        )

    # -- candidate selection ---------------------------------------------------

    def _explicit(
        self, item: ManifestItem, catalog: SourceCatalog
    ) -> tuple[list[ResolvedSource], list[dict[str, Any]]] | None:
        selection = catalog.latest_selection(item.id)
        if selection is None:
            return None
        sources: list[ResolvedSource] = []
        blockers: list[dict[str, Any]] = []
        for reference in selection:
            kind = reference.get("kind")
            source_id = reference.get("id")
            row = (
                catalog.documents.get(source_id)
                if kind == "document"
                else catalog.communications.get(source_id)
            )
            if row is None:
                blockers.append(
                    {"code": "missing_source", "item_id": item.id, "detail": str(source_id)}
                )
                continue
            resolved, reason = (
                self._document_source(row)
                if kind == "document"
                else self._communication_source(row)
            )
            if resolved is None:
                blockers.append({"code": reason, "item_id": item.id, "detail": str(source_id)})
                continue
            sources.append(resolved)
        return sources, blockers

    def _auto_doc_type(
        self, item: ManifestItem, catalog: SourceCatalog
    ) -> tuple[list[ResolvedSource], list[dict[str, Any]]]:
        sources: list[ResolvedSource] = []
        blockers: list[dict[str, Any]] = []
        for row in catalog.documents.values():
            if row["doc_type"] not in item.doc_types or row["status"] == "rejected":
                continue
            resolved, reason = self._document_source(row)
            if resolved is None:
                blockers.append({"code": reason, "item_id": item.id, "detail": row["id"]})
                continue
            sources.append(resolved)
        return sources, blockers

    def _selected_assessor_report(
        self, item: ManifestItem, catalog: SourceCatalog
    ) -> tuple[list[ResolvedSource], list[dict[str, Any]]]:
        document_id: str | None = None
        for event in catalog.selected_reports:
            candidate = event["payload"].get("selected_document_id")
            if isinstance(candidate, str):
                document_id = candidate
        candidates = sorted(
            {
                event["payload"]["document_id"]
                for event in catalog.report_events
                if isinstance(event["payload"].get("document_id"), str)
            }
        )
        if document_id is None:
            if len(candidates) > 1:
                return [], [
                    {
                        "code": "ambiguous_sources",
                        "item_id": item.id,
                        "detail": ",".join(candidates),
                    }
                ]
            document_id = candidates[0] if candidates else None
        if document_id is None:
            return [], []
        row = catalog.documents.get(document_id)
        if row is None:
            return [], [
                {"code": "missing_source", "item_id": item.id, "detail": document_id}
            ]
        resolved, reason = self._document_source(row)
        if resolved is None:
            return [], [{"code": reason, "item_id": item.id, "detail": document_id}]
        return [resolved], []

    def _projection_or_upload(
        self, item: ManifestItem, catalog: SourceCatalog
    ) -> tuple[list[ResolvedSource], list[dict[str, Any]], bool]:
        payload = catalog.latest_upload(item.id)
        if payload is None:
            # PRD-09 projection readback is a declared seam (register #224). It is
            # visibly pending rather than guessed; officer upload is live today.
            return [], [], True
        resolved, reason = self._upload_source(payload)
        if resolved is None:
            return [], [{"code": reason, "item_id": item.id, "detail": item.id}], False
        return [resolved], [], False

    # -- validity --------------------------------------------------------------

    def _validate(
        self, item: ManifestItem, sources: list[ResolvedSource]
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for source in sources:
            conversion = item.conversion
            if conversion == "source_default":
                conversion = "html_to_pdf" if source.kind == "communication" else "passthrough"
            if conversion == "passthrough":
                try:
                    content = self.app.state.blob_store.get(source.blob_key)
                except Exception:  # noqa: BLE001 - unreadable bytes are a blocker
                    blockers.append(
                        {"code": "missing_blob", "item_id": item.id, "detail": source.id}
                    )
                    continue
                if not is_pdf(content):
                    blockers.append(
                        {
                            "code": "conversion_unsupported",
                            "item_id": item.id,
                            "detail": source.id,
                        }
                    )
                    continue
                try:
                    page_count(content)
                except Exception:  # noqa: BLE001 - an unparseable PDF is a blocker
                    blockers.append(
                        {"code": "invalid_pdf", "item_id": item.id, "detail": source.id}
                    )
            elif conversion == "photos_2up" and source.mime not in IMAGE_MIMES:
                blockers.append(
                    {"code": "conversion_unsupported", "item_id": item.id, "detail": source.id}
                )
        return blockers

    def resolve_item(self, item: ManifestItem, catalog: SourceCatalog) -> ItemResolution:
        """Resolve one row to exactly one explicit state, never a default."""

        pending = False
        if item.selector == "projection_or_upload":
            sources, blockers, pending = self._projection_or_upload(item, catalog)
        else:
            explicit = self._explicit(item, catalog)
            if explicit is not None:
                sources, blockers = explicit
            elif item.selector == "auto_doc_type":
                sources, blockers = self._auto_doc_type(item, catalog)
            elif item.selector == "selected_assessor_report":
                sources, blockers = self._selected_assessor_report(item, catalog)
            else:
                sources, blockers = [], []
        sources = sorted(sources, key=lambda source: (source.received_at, source.kind, source.id))
        explicit_order = catalog.latest_selection(item.id)
        if explicit_order is not None:
            order = {
                str(reference.get("id")): index
                for index, reference in enumerate(explicit_order)
            }
            sources = sorted(sources, key=lambda source: order.get(source.id, len(order)))
        blockers = list(blockers)
        blockers.extend(self._validate(item, sources))
        if pending:
            state = "pending_integration"
            blockers.append(
                {
                    "code": "pending_integration",
                    "item_id": item.id,
                    "detail": "projection readback pending PRD-09; upload the PDF instead",
                }
            )
        elif blockers:
            state = "invalid"
        elif not sources:
            state = "missing"
            blockers.append({"code": "missing_sources", "item_id": item.id, "detail": item.id})
        elif not item.repeatable and len(sources) > 1:
            state = "ambiguous"
            blockers.append(
                {
                    "code": "ambiguous_sources",
                    "item_id": item.id,
                    "detail": ",".join(sorted(source.id for source in sources)),
                }
            )
        else:
            state = "ready"
        return ItemResolution(item=item, state=state, sources=sources, blockers=blockers)


class ReadinessEngine:
    """Recompute the complete readiness card from current durable inputs."""

    def __init__(self, app: Any, config: ApprovalPackConfig) -> None:
        self.app = app
        self.config = config
        self.resolver = ManifestResolver(app, config)

    def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(text(sql), params).mappings()
            return [dict(row) for row in rows]

    def _checklists(self, claim_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rows = self._rows(
            "SELECT id, purpose, status FROM chase_checklists WHERE claim_id = :claim_id "
            "ORDER BY created_at, id",
            claim_id=claim_id,
        )
        relevant = [row for row in rows if row["purpose"] in {"claim_docs", "assessor_report"}]
        blockers: list[dict[str, Any]] = []
        if not any(row["purpose"] == "claim_docs" for row in relevant):
            blockers.append(
                {
                    "code": "checklist_missing",
                    "item_id": None,
                    "detail": "no claim_docs checklist exists",
                }
            )
        for row in relevant:
            if row["status"] != "complete":
                blockers.append(
                    {"code": "checklist_incomplete", "item_id": None, "detail": row["id"]}
                )
        card = {"ready": not blockers, "blockers": list(blockers)}
        return card, blockers

    def _required_fields(
        self, claim_id: str, actor: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        paths = self.required_field_paths(claim_id, actor)
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=paths
        )
        blockers: list[dict[str, Any]] = []
        card_blockers: list[dict[str, Any]] = []
        for path in paths:
            row = fields.get(path)
            if row is None:
                card_blockers.append(
                    {"path": path, "code": "missing", "required": VERIFICATION_FLOOR}
                )
                blockers.append(
                    {"code": "field_missing", "item_id": None, "detail": path}
                )
            elif row.verification_state != VERIFICATION_FLOOR:
                card_blockers.append(
                    {"path": path, "code": "under_verified", "required": VERIFICATION_FLOOR}
                )
                blockers.append(
                    {"code": "field_under_verified", "item_id": None, "detail": path}
                )
        card = {"ready": not card_blockers, "blockers": card_blockers}
        return card, blockers, fields

    def required_field_paths(self, claim_id: str, actor: str) -> list[str]:
        """Return T-01's active required fields from the pack template registry."""

        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        pack_id, separator, version = claim.pack_version.partition("@")
        if not separator:
            raise LookupError("claim pack pin is malformed")
        definition = self.app.state.cop_runtime.template_registry(pack_id, version).get("T-01")
        return list(definition.required_fields)

    def _open_exceptions(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT id, subtype FROM review_items WHERE claim_id = :claim_id "
            "AND type = 'EXCEPTION' AND status = 'open' ORDER BY created_at, id",
            claim_id=claim_id,
        )
        return [
            {"code": "open_exception", "item_id": None, "detail": row["id"]}
            for row in rows
            if row["subtype"] in BLOCKING_EXCEPTION_SUBTYPES
        ]

    def _latest_versions(self, claim_id: str) -> tuple[int, int, str | None]:
        merged = self._rows(
            "SELECT payload FROM events WHERE claim_id = :claim_id AND type = 'pack.merged' "
            "ORDER BY seq",
            claim_id=claim_id,
        )
        pack_version = 0
        for row in merged:
            payload = _json(row["payload"])
            candidate = payload.get("version")
            if isinstance(candidate, int) and candidate > pack_version:
                pack_version = candidate
        drafts = self._rows(
            "SELECT version, status FROM note_drafts WHERE claim_id = :claim_id "
            "ORDER BY version",
            claim_id=claim_id,
        )
        note_version = drafts[-1]["version"] if drafts else 0
        note_status = drafts[-1]["status"] if drafts else None
        return pack_version, note_version, note_status

    def evaluate(self, claim_id: str, actor: str = ACTOR) -> Readiness:
        """Return the complete card. This is a pure read: it emits nothing."""

        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        pack_version, note_version, note_status = self._latest_versions(claim_id)
        blockers: list[dict[str, Any]] = []
        if claim.status not in READY_STATES or (
            claim.status == "PACK_READY" and note_status == "signed"
        ):
            blockers.append(
                {
                    "code": "claim_state_not_eligible",
                    "item_id": None,
                    "detail": claim.status,
                }
            )
        checklist_card, checklist_blockers = self._checklists(claim_id)
        blockers.extend(checklist_blockers)

        catalog = SourceCatalog(self.app, claim_id)
        items = [self.resolver.resolve_item(item, catalog) for item in self.config.items]
        for item in items:
            blockers.extend(item.blockers)

        field_card, field_blockers, field_rows = self._required_fields(claim_id, actor)
        blockers.extend(field_blockers)
        blockers.extend(self._open_exceptions(claim_id))

        material = {
            "checklists": self._rows(
                "SELECT id, purpose, status FROM chase_checklists WHERE claim_id = :claim_id "
                "ORDER BY id",
                claim_id=claim_id,
            ),
            "consistency": [
                row["id"]
                for row in self._rows(
                    "SELECT id FROM consistency_results WHERE claim_id = :claim_id ORDER BY id",
                    claim_id=claim_id,
                )
            ],
            "fields": [
                [path, row.id, row.version]
                for path, row in sorted(field_rows.items())
            ],
            "items": [
                [
                    resolution.item.id,
                    resolution.state,
                    [[source.kind, source.id, source.sha256] for source in resolution.sources],
                ]
                for resolution in items
            ],
            "manifest_version": self.config.manifest_version,
            "note_version": note_version,
            "pack_version": pack_version,
            "savings": self._rows(
                "SELECT id, saving FROM savings_ledger WHERE claim_id = :claim_id ORDER BY id",
                claim_id=claim_id,
            ),
            "status": claim.status,
        }
        fingerprint = hashlib.sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()
        return Readiness(
            claim_id=claim_id,
            status=claim.status,
            ready=not blockers,
            fingerprint=fingerprint,
            checklists=checklist_card,
            fields=field_card,
            items=items,
            blockers=blockers,
            field_rows=field_rows,
        )


__all__ = [
    "ACTOR",
    "ItemResolution",
    "ManifestResolver",
    "Readiness",
    "ReadinessEngine",
    "ResolvedSource",
    "SourceCatalog",
]
