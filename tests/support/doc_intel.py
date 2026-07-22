"""Doc-intel engine builder for ordinary (non-acceptance) tests.

Two things every editable harness wants and must not get wrong:

* a deterministic OCR engine, because the production default is the real
  Tesseract binary (`engine.py`);
* a test-tier page raster DPI. NORMALIZE renders every page at
  `page_raster_dpi`, and cost scales with its square — 300 DPI is the
  extraction-quality value and stays the default everywhere else, including
  the protected acceptance scenarios.

Both are overrides, not redefinitions: pass `ocr_engine` or `model_config` to
take control back.
"""
from __future__ import annotations

from typing import Any

from doc_intel.engine import DocIntelEngine, build_engine
from support.ocr import DeterministicOcrEngine

#: Enough to raster a page and run word boxes over it, ~10x cheaper than 300.
UNIT_PAGE_RASTER_DPI = 96


def build_doc_intel_engine(
    app: Any,
    *,
    model_client: Any,
    ocr_engine: Any | None = None,
    model_config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> DocIntelEngine:
    """`build_engine` with the test-tier OCR engine and raster DPI applied."""

    merged: dict[str, Any] = {"page_raster_dpi": UNIT_PAGE_RASTER_DPI}
    if model_config is not None:
        merged.update(model_config)
    return build_engine(
        app,
        model_client=model_client,
        ocr_engine=ocr_engine if ocr_engine is not None else DeterministicOcrEngine(),
        model_config=merged,
        **kwargs,
    )
