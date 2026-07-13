"""Fail-closed vision-bbox eligibility and render-crop verification."""

from __future__ import annotations

import io
import math
import re
from typing import Any

from PIL import Image

from doc_intel.settings import DEFAULTS


def normalized_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(item)
        or item < 0
        or item > 1
        for item in value
    ):
        return None
    bbox = [float(item) for item in value]
    return bbox if bbox[0] < bbox[2] and bbox[1] < bbox[3] else None


def eligible(
    *, handwritten: bool, text_coverage: float, threshold: float | None = None
) -> bool:
    configured = (
        float(DEFAULTS["vision_text_coverage_threshold"])
        if threshold is None
        else threshold
    )
    return handwritten or text_coverage < configured


def crop_png(page_png: bytes, bbox: list[float]) -> bytes:
    image = Image.open(io.BytesIO(page_png))
    width, height = image.size
    pixels = (
        int(bbox[0] * width),
        int(bbox[1] * height),
        max(int(bbox[2] * width), int(bbox[0] * width) + 1),
        max(int(bbox[3] * height), int(bbox[1] * height) + 1),
    )
    output = io.BytesIO()
    image.crop(pixels).save(output, format="PNG")
    return output.getvalue()


def native_candidate_outside_bbox(
    *, value: Any, words: list[dict[str, Any]], bbox: list[float]
) -> bool:
    """Reject a bbox when the native layer locates its candidate somewhere else."""

    candidate_tokens = {
        token
        for raw in str(value).split()
        if len(token := re.sub(r"[^A-Z0-9]", "", raw.upper())) >= 2
    }
    matching_boxes = [
        word.get("bbox")
        for word in words
        if word.get("source") == "native"
        and re.sub(r"[^A-Z0-9]", "", str(word.get("text", "")).upper())
        in candidate_tokens
        and normalized_bbox(word.get("bbox")) is not None
    ]
    if not matching_boxes:
        return False
    return all(
        box[2] <= bbox[0]
        or box[0] >= bbox[2]
        or box[3] <= bbox[1]
        or box[1] >= bbox[3]
        for box in matching_boxes
    )


def verify_crop(
    *,
    value: Any,
    crop_png_bytes: bytes,
    model_client: Any,
    claim_id: str | None = None,
    document_id: str | None = None,
) -> tuple[bool, float]:
    result = model_client.structured_call(
        tier="MODEL_LIGHT",
        schema={
            "type": "object",
            "required": ["visible"],
            "additionalProperties": False,
            "properties": {"visible": {"type": "boolean"}},
        },
        inputs={
            "task": "vision_crop_verify",
            "_claim_id": claim_id,
            "_document_id": document_id,
            "value": value,
            "crop_png": crop_png_bytes,
        },
    )
    return result["data"].get("visible") is True, float(result["cost_usd"])
