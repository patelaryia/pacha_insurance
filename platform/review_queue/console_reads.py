"""Read-only Claim-360 aggregation and append-only citation resolution."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import fitz

from claim_core import ClaimCoreError
from review_queue.models import ReviewItem


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _citation_detail(source_type: str, source_ref: Any) -> dict[str, Any] | None:
    if not isinstance(source_ref, dict):
        return None
    is_extraction = source_type == "extraction"
    is_review_verified = (
        source_type == "human"
        and source_ref.get("review_verified") is True
        and isinstance(source_ref.get("review_item_id"), str)
    )
    if not is_extraction and not is_review_verified:
        return None
    document_id = source_ref.get("document_id")
    page = source_ref.get("page")
    bbox = source_ref.get("bbox")
    if not isinstance(document_id, str) or not document_id:
        return None
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    if any(
        not isinstance(coordinate, (int, float))
        or isinstance(coordinate, bool)
        or not math.isfinite(coordinate)
        or coordinate < 0
        or coordinate > 1
        for coordinate in bbox
    ):
        return None
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        return None
    return {"document_id": document_id, "page": page, "bbox": list(bbox)}


class ConsoleReadService:
    """Compose console reads through package public interfaces only."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.claims = app.state.claim_service
        self.blobs = app.state.blob_store

    def _review_evidence_valid(
        self,
        *,
        claim_id: str,
        path: str,
        source_ref: dict[str, Any],
        detail: dict[str, Any],
    ) -> bool:
        if source_ref.get("review_verified") is not True:
            return True
        review_id = source_ref.get("review_item_id")
        if not isinstance(review_id, str):
            return False
        service = self.app.state.review_queue.service
        with service.sessions() as session:
            item = session.get(ReviewItem, review_id)
            if (
                item is None
                or item.type != "FIELD_VERIFY"
                or item.status != "resolved"
                or item.claim_id != claim_id
                or item.payload.get("path") != path
            ):
                return False
            citation = item.payload.get("citation")
            return (
                item.payload.get("document_id") == detail["document_id"]
                and item.payload.get("page") == detail["page"]
                and isinstance(citation, dict)
                and citation.get("bbox") == detail["bbox"]
            )

    def _lineage_citation(
        self,
        claim_id: str,
        path: str,
        *,
        documents: dict[str, Any],
        require_render: bool,
    ) -> dict[str, Any] | None:
        lineage = self.claims.field_citation_lineage(claim_id, paths=[path]).get(path, [])
        for version in lineage:
            detail = _citation_detail(version["source_type"], version["source_ref"])
            if detail is None:
                continue
            if not self._review_evidence_valid(
                claim_id=claim_id,
                path=path,
                source_ref=version["source_ref"],
                detail=detail,
            ):
                continue
            document = documents.get(detail["document_id"])
            if document is None or document.claim_id != claim_id:
                continue
            key = f"normalised/{document.id}.pdf"
            if require_render and not self.blobs.exists(key):
                continue
            return detail
        return None

    def claim_360(self, claim_id: str, actor: str) -> dict[str, Any]:
        claim, fields, _blocked = self.claims.hydrate_claim(claim_id, actor)
        documents = self.claims.documents(claim_id)
        document_by_id = {row.id: row for row in documents}
        lineage = self.claims.field_citation_lineage(claim_id)

        def has_citation(path: str) -> bool:
            for version in lineage.get(path, []):
                detail = _citation_detail(version["source_type"], version["source_ref"])
                if detail is None:
                    continue
                if not self._review_evidence_valid(
                    claim_id=claim_id,
                    path=path,
                    source_ref=version["source_ref"],
                    detail=detail,
                ):
                    continue
                document = document_by_id.get(detail["document_id"])
                if (
                    document is not None
                    and document.claim_id == claim_id
                    and self.blobs.exists(f"normalised/{document.id}.pdf")
                ):
                    return True
            return False

        field_rows = [
            {
                "path": path,
                "value": field.value,
                "value_type": field.value_type,
                "verification_state": field.verification_state,
                "confidence": (
                    None if field.confidence is None else float(field.confidence)
                ),
                "source_type": field.source_type,
                "has_citation": has_citation(path),
            }
            for path, field in sorted(fields.items())
        ]
        financials = []
        for path, field in sorted(fields.items()):
            if field.value_type != "money":
                continue
            if not isinstance(field.value, int) or isinstance(field.value, bool):
                continue
            source_ref = field.source_ref if isinstance(field.source_ref, dict) else {}
            calc_run_id = source_ref.get("calc_run_id")
            financials.append(
                {
                    "path": path,
                    "amount_cents": str(field.value),
                    "calc_run_id": calc_run_id if isinstance(calc_run_id, str) else None,
                }
            )

        def field_value(path: str) -> Any:
            field = fields.get(path)
            return None if field is None else field.value

        routing = fields.get("settlement.payable") or fields.get("reserve.total")
        amount = None
        if (
            routing is not None
            and routing.value_type == "money"
            and isinstance(routing.value, int)
            and not isinstance(routing.value, bool)
        ):
            amount = str(routing.value)

        timeline_events = self.claims.timeline(claim_id)
        timeline = [
            {
                "id": event.id,
                "seq": event.seq,
                "event_type": event.type,
                "payload": event.payload,
                "actor": event.actor,
                "correlation_id": event.correlation_id,
                "occurred_at": _iso(event.occurred_at),
            }
            for event in timeline_events
        ]
        systems = [
            {"event_type": event.type, **event.payload}
            for event in timeline_events
            if event.type.startswith("projection.")
        ]
        communications = [
            {"event_type": event.type, **event.payload}
            for event in timeline_events
            if event.type.startswith("email.")
        ]
        return {
            "claim": {
                "id": claim.id,
                "status": claim.status,
                "substatus": claim.substatus,
                "assigned_to": claim.assigned_to,
                "created_at": _iso(claim.created_at),
                "updated_at": _iso(claim.updated_at),
            },
            "header": {
                "insured": field_value("parties.insured.name"),
                "registration": field_value("vehicle.reg"),
                "amount_cents": amount,
            },
            "fields": field_rows,
            "documents": [
                {
                    "id": document.id,
                    "claim_id": document.claim_id,
                    "parent_document_id": document.parent_document_id,
                    "doc_type": document.doc_type,
                    "status": document.status,
                    "filename": document.filename,
                    "mime": document.mime,
                    "sha256": document.sha256,
                    "page_count": document.page_count,
                    "source": document.source,
                    "received_at": _iso(document.received_at),
                }
                for document in documents
            ],
            "financials": financials,
            "timeline": timeline,
            "systems": systems,
            "communications": communications,
            "availability": {
                "document_checklist": {"status": "not_available", "owner": "PRD-06"},
                "systems": {
                    "status": "available" if systems else "not_available",
                    "owner": "PRD-09",
                },
                "communications": {
                    "status": "available" if communications else "not_available",
                    "owner": "PRD-06",
                },
            },
        }

    def citation(self, claim_id: str, field_path: str, actor: str) -> dict[str, Any]:
        _claim, fields, _blocked = self.claims.hydrate_claim(
            claim_id, actor, paths=[field_path]
        )
        current = fields.get(field_path)
        if current is None:
            self._citation_unavailable()
        documents = {row.id: row for row in self.claims.documents(claim_id)}
        detail = self._lineage_citation(
            claim_id,
            field_path,
            documents=documents,
            require_render=True,
        )
        if detail is None:
            self._citation_unavailable()
        key = f"normalised/{detail['document_id']}.pdf"
        try:
            content = self.blobs.get(key)
            pdf = fitz.open(stream=content, filetype="pdf")
            page_count = pdf.page_count
            pdf.close()
        except Exception:
            self._citation_unavailable()
        if detail["page"] > page_count:
            self._citation_unavailable()
        return {
            "claim_id": claim_id,
            "field_path": field_path,
            "value": current.value,
            "value_type": current.value_type,
            "verification_state": current.verification_state,
            "document_id": detail["document_id"],
            "page": detail["page"],
            "bbox": detail["bbox"],
            "document_url": (
                f"/console/documents/{detail['document_id']}/normalised.pdf"
            ),
        }

    def normalised_pdf(self, document_id: str) -> bytes:
        document = self.claims.get_document(document_id)
        # Reading the claim enforces that the document is attached to a real claim.
        self.claims.documents(document.claim_id)
        key = f"normalised/{document.id}.pdf"
        try:
            content = self.blobs.get(key)
            pdf = fitz.open(stream=content, filetype="pdf")
            is_pdf = pdf.is_pdf and pdf.page_count > 0
            pdf.close()
        except Exception as error:
            raise ClaimCoreError(
                404,
                "NORMALISED_PDF_NOT_FOUND",
                "Normalised PDF is unavailable",
            ) from error
        if not is_pdf:
            raise ClaimCoreError(
                404,
                "NORMALISED_PDF_NOT_FOUND",
                "Normalised PDF is unavailable",
            )
        return content

    @staticmethod
    def _citation_unavailable() -> None:
        raise ClaimCoreError(
            409,
            "CITATION_UNAVAILABLE",
            "A valid same-claim citation render is unavailable",
        )


__all__ = ["ConsoleReadService"]
