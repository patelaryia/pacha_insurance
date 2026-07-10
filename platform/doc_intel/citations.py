"""Mandatory anchor-text citation resolution for text-layer pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz.distance import Levenshtein


@dataclass(frozen=True)
class CitationMatch:
    bbox: list[float]
    score: float
    start_word: int
    end_word: int


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def match_anchor(
    anchor_text: str,
    words: list[dict[str, Any]],
    *,
    threshold: float = 0.85,
) -> CitationMatch | None:
    """Fuzzy-match an anchor over token-count ±2 sliding windows."""

    if not anchor_text or len(anchor_text) > 120 or not words:
        return None
    anchor = _normalise(anchor_text)
    token_count = len(anchor.split())
    best: tuple[float, int, int] | None = None
    for size in range(max(1, token_count - 2), token_count + 3):
        if size > len(words):
            continue
        for start in range(0, len(words) - size + 1):
            end = start + size
            candidate = _normalise(" ".join(str(word.get("text", "")) for word in words[start:end]))
            score = Levenshtein.normalized_similarity(anchor, candidate)
            if best is None or score > best[0]:
                best = (score, start, end)
    if best is None or best[0] < threshold:
        return None
    _, start, end = best
    boxes = [word.get("bbox") for word in words[start:end]]
    if any(not isinstance(box, list) or len(box) != 4 for box in boxes):
        return None
    bbox = [
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    ]
    return CitationMatch(bbox=bbox, score=best[0], start_word=start, end_word=end)
