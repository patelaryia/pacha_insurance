"""Fail-closed derived Swahili remarks-gloss artifact."""

from __future__ import annotations

import json
from typing import Any


def create_remarks_gloss(
    *,
    document_id: str,
    remarks: str,
    model_client: Any,
    blob_store: Any,
    claim_id: str | None = None,
) -> float:
    result = model_client.structured_call(
        tier="MODEL_LIGHT",
        schema={
            "type": "object",
            "required": ["gloss"],
            "additionalProperties": False,
            "properties": {"gloss": {"type": "string"}},
        },
        inputs={
            "task": "translate_swahili_gloss",
            "_claim_id": claim_id,
            "_document_id": document_id,
            "remarks": remarks,
        },
    )
    payload = {
        "source_field": "remarks",
        "value": result["data"]["gloss"],
        "machine_translated": True,
        "rule_input": False,
        "status": "pending_field_registration",
    }
    blob_store.put(
        f"derived/{document_id}/remarks_gloss.json",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
    )
    return float(result["cost_usd"])
