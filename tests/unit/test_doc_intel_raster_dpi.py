"""NORMALIZE's page raster resolution is configuration, and it fails closed.

The 300-DPI default is an extraction-quality value and is unchanged. What is
new is that a caller may ask for another one, so these pin the default, the
smoke path at 300, and every way a bad value must refuse rather than guess.
"""
from __future__ import annotations

import io

import fitz
import pytest

from support.ocr import DeterministicOcrEngine


def _one_page_pdf() -> bytes:
    """A single page exactly 72pt square, so 1pt renders as 1px per DPI unit."""
    document = fitz.open()
    document.new_page(width=72, height=72)
    data = document.tobytes()
    document.close()
    return data


def _normalise(tmp_path, *, dpi=None, document_id="01K00000000000000000000010"):
    from claim_core.storage import LocalBlobStore
    from doc_intel.normalize import normalise_document

    store = LocalBlobStore(tmp_path)
    store.put("page.pdf", _one_page_pdf())
    ocr = DeterministicOcrEngine()
    kwargs = {} if dpi is None else {"page_raster_dpi": dpi}
    result = normalise_document(
        document_id=document_id,
        filename="page.pdf",
        mime="application/pdf",
        source_key="page.pdf",
        blob_store=store,
        ocr_engine=ocr,
        **kwargs,
    )
    return result, store, ocr


def _png_size(store, key: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(store.get(key))) as image:
        return image.size


# --- the configured default -----------------------------------------------

def test_default_renders_at_the_configured_three_hundred_dpi(tmp_path):
    """The 300-DPI smoke path: real dimensions, and OCR ran over the raster."""
    result, store, ocr = _normalise(tmp_path)

    assert result.page_count == 1
    assert _png_size(store, result.page_keys[0]) == (300, 300)
    assert ocr.calls == 1


def test_explicit_three_hundred_matches_the_default(tmp_path):
    _, store_default, _ = _normalise(tmp_path / "a")
    _, store_explicit, _ = _normalise(tmp_path / "b", dpi=300)

    assert _png_size(store_default, "pages/01K00000000000000000000010/1.png") == _png_size(
        store_explicit, "pages/01K00000000000000000000010/1.png"
    )


# --- the test tier ----------------------------------------------------------

def test_lower_dpi_renders_a_proportionally_smaller_raster(tmp_path):
    from support.doc_intel import UNIT_PAGE_RASTER_DPI

    result, store, ocr = _normalise(tmp_path, dpi=UNIT_PAGE_RASTER_DPI)

    assert _png_size(store, result.page_keys[0]) == (
        UNIT_PAGE_RASTER_DPI,
        UNIT_PAGE_RASTER_DPI,
    )
    assert ocr.calls == 1


# --- fail closed ------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, -300])
def test_non_positive_dpi_refuses_rather_than_defaulting(tmp_path, bad):
    from doc_intel.normalize import NormaliseError

    with pytest.raises(NormaliseError, match="must be positive"):
        _normalise(tmp_path, dpi=bad)


@pytest.mark.parametrize("bad", [96.0, "96", True])
def test_non_integer_dpi_refuses_rather_than_coercing(tmp_path, bad):
    from doc_intel.normalize import NormaliseError

    with pytest.raises(NormaliseError, match="must be an integer"):
        _normalise(tmp_path, dpi=bad)


# --- the value is pack configuration, not a literal -------------------------

def test_engine_reads_the_dpi_from_pack_configuration(tmp_path):
    """A pack-level override reaches NORMALIZE without touching engine code."""
    from doc_intel.normalize import _page_raster_dpi

    assert _page_raster_dpi(None) == 300
    assert _page_raster_dpi(96) == 96
