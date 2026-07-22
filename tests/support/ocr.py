"""The deterministic OCR engine every ordinary test harness injects.

`DocIntelEngine` falls back to a real `TesseractOcrEngine` whenever a caller
omits `ocr_engine` (`engine.py`), so a harness that forgets to inject one
silently shells out to the binary: slow, non-deterministic, and dependent on
whatever Tesseract version the runner happens to carry.

Injection is deliberately explicit rather than a global monkeypatch. The two
ordinary test jobs run without the binary installed, so an omitted injection
fails there instead of passing quietly on a machine that happens to have it.
Tests that mean to exercise the real adapter carry the `real_ocr` marker and
run in the `ocr-smoke` job.
"""
from __future__ import annotations

from typing import Any

DEFAULT_WORDS: tuple[dict[str, Any], ...] = (
    {"text": "OCR-WORD", "bbox": [0.10, 0.10, 0.30, 0.15]},
)


class DeterministicOcrEngine:
    """An `OcrEngine` that returns fixed word boxes and counts its calls."""

    def __init__(self, words: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.result = [dict(word) for word in (words if words is not None else DEFAULT_WORDS)]

    def words(self, page_png_bytes: bytes) -> list[dict[str, Any]]:
        if not page_png_bytes:
            raise AssertionError("NORMALIZE handed OCR an empty page raster")
        self.calls += 1
        return [dict(word) for word in self.result]
