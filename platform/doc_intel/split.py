"""Page-level split detection helpers and strict human boundary application."""

from __future__ import annotations

import io
from typing import Any

import fitz

from claim_core.errors import ClaimCoreError


def validate_boundaries(boundaries: list[dict[str, Any]], page_count: int) -> list[tuple[int, int]]:
    if len(boundaries) < 2:
        raise ClaimCoreError(422, "INVALID_SPLIT_BOUNDARY", "A split requires two children")
    parsed = []
    for boundary in boundaries:
        start = boundary.get("start_page")
        end = boundary.get("end_page")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
        ):
            raise ClaimCoreError(422, "INVALID_SPLIT_BOUNDARY", "Invalid page range")
        parsed.append((start, end))
    expected = 1
    for start, end in parsed:
        if start != expected:
            raise ClaimCoreError(
                422,
                "INVALID_SPLIT_BOUNDARY",
                "Boundaries must cover every page exactly once in order",
            )
        expected = end + 1
    if expected != page_count + 1:
        raise ClaimCoreError(
            422, "INVALID_SPLIT_BOUNDARY", "Boundaries must cover every parent page"
        )
    return parsed


def pdf_subset(pdf_bytes: bytes, start_page: int, end_page: int) -> bytes:
    source = fitz.open(stream=pdf_bytes, filetype="pdf")
    child = fitz.open()
    try:
        child.insert_pdf(source, from_page=start_page - 1, to_page=end_page - 1)
        output = io.BytesIO()
        child.save(output, garbage=4, deflate=True)
        return output.getvalue()
    finally:
        child.close()
        source.close()
