"""Citation-gated batch commit through claim_core's public write path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from claim_core import BlobStore, ClaimService, FieldWrite


@dataclass(frozen=True)
class CommitResult:
    committed_paths: list[str]
    reviews: list[dict[str, Any]]


def prepare_commit(
    *,
    document_id: str,
    doc_type: str,
    fields: list[dict[str, Any]],
    schema: dict[str, Any],
    blob_store: BlobStore,
    review_capability_id: str,
) -> tuple[list[FieldWrite], list[dict[str, Any]]]:
    """Separate provenance-backed writes from review candidates."""

    writes = []
    reviews = []
    for field in fields:
        definition = schema["fields"][field["name"]]
        target = definition.get("target_path")
        if target is None:
            continue
        citation = field.get("citation")
        threshold = Decimal(str(field["threshold"]))
        combined = Decimal(str(field["combined_confidence"]))
        commit_eligible = (
            citation is not None
            and not field.get("citation_failed", False)
            and not definition.get("always_review", False)
            and field.get("validator_outcome") != "out_of_scope"
            and combined >= threshold
        )
        if commit_eligible:
            if field.get("citation_mode") == "vision_bbox":
                source_ref = {
                    "document_id": document_id,
                    "page": field["page"],
                    "bbox": citation["bbox"],
                    "citation_mode": "vision_bbox",
                    "vision_verified": citation.get("vision_verified") is True,
                }
            else:
                source_ref = {
                    "document_id": document_id,
                    "page": field["page"],
                    "bbox": citation["bbox"],
                    "anchor_text": field["anchor_text"],
                }
            writes.append(
                FieldWrite(
                    path=target,
                    value=field["normalised_value"],
                    value_type=(
                        "money" if definition["validator"] == "money_kes" else definition["type"]
                    ),
                    source_type="extraction",
                    source_ref=source_ref,
                    confidence=combined,
                    verification_state="extracted",
                    pii_class=definition["pii_class"],
                )
            )
            continue
        payload: dict[str, Any] = {
            "type": "FIELD_VERIFY",
            "capability_id": review_capability_id,
            "document_id": document_id,
            "path": target,
            "candidate_value": field["normalised_value"],
            "value_type": (
                "money" if definition["validator"] == "money_kes" else definition["type"]
            ),
            "combined_confidence": float(combined),
            "page": field.get("page"),
            "citation_mode": field.get("citation_mode", "anchor_text"),
            "citation": citation or {"citation_failed": True},
        }
        if definition["pii_class"] != "none":
            blob_key = f"review-candidates/{document_id}/{field['name']}.json"
            blob_store.put(
                blob_key,
                json.dumps(field["normalised_value"], ensure_ascii=False).encode("utf-8"),
            )
            payload["candidate_value"] = "__redacted__"
            payload["candidate_blob_ref"] = blob_key
        reviews.append(payload)
    return writes, reviews


def commit_writes(
    *,
    service: ClaimService,
    claim_id: str,
    document_id: str,
    writes: list[FieldWrite],
) -> list[str]:
    """Use claim_core's append-only, dictionary-enforcing public write path."""

    service.write_document_extractions(
        claim_id, document_id, writes, actor="agent:doc_intel"
    )
    return [write.path for write in writes]
