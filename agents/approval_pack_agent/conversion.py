"""Deterministic, offline source conversion and the immutable-store boundary.

No converter reaches the network. The HTML renderer is an injected protocol so
acceptance never needs a live browser, and the immutable store is Object-Lock
shaped so the production adapter is a drop-in (register #226).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import fitz

from approval_pack_agent.config import (
    HtmlRenderPolicy,
    canonical_json,
    eat_date,
)

A4_WIDTH = 595.0
A4_HEIGHT = 842.0
MM_TO_POINTS = 72.0 / 25.4
FALLBACK_REASONS = {
    TimeoutError: "html_renderer_timeout",
}


class ConversionFailed(RuntimeError):
    """A source could not be converted; the merge must fail closed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class HtmlPdfRenderer(Protocol):
    """Offline HTML to PDF renderer supplied by infrastructure or a test."""

    def render(self, html: str, *, policy: HtmlRenderPolicy) -> Any:
        """Return an object carrying pdf_bytes and blocked-resource counters."""


class ImmutableArtifactStore(Protocol):
    """Write-once artifact boundary; never call a mutable put from merge code."""

    def put_immutable(self, key: str, content: bytes, *, retention: str) -> None:
        """Store bytes that may never be replaced with different bytes."""

    def get(self, key: str) -> bytes:
        """Read previously stored bytes."""

    def exists(self, key: str) -> bool:
        """Return whether the key already holds bytes."""


class LocalImmutableStore:
    """Write-once adapter over the claim-core blob store.

    Real S3 Object Lock remains register #30/#116. This adapter reports
    ``local_write_once`` so production publishability stays visibly blocked.
    """

    object_lock_status = "local_write_once"

    def __init__(self, blob_store: Any) -> None:
        self._blobs = blob_store

    def put_immutable(self, key: str, content: bytes, *, retention: str) -> None:
        if not retention:
            raise ValueError("immutable writes require a retention class")
        if self._blobs.exists(key):
            if self._blobs.get(key) != content:
                raise ConversionFailed(
                    "immutable_overwrite_refused",
                    f"{key} already holds different bytes",
                )
            return
        self._blobs.put(key, content)

    def get(self, key: str) -> bytes:
        return self._blobs.get(key)

    def exists(self, key: str) -> bool:
        return self._blobs.exists(key)


@dataclass(frozen=True)
class CommunicationArchive:
    """The archived message fields a deterministic fallback may render."""

    subject: str
    from_addr: str
    to_addrs: tuple[str, ...]
    occurred_at: datetime
    body: str


@dataclass(frozen=True)
class Converted:
    """One converted source ready for concatenation."""

    pdf_bytes: bytes
    page_count: int
    source_pages: int
    fallback_used: bool
    fallback_reason: str | None
    blocked_resource_count: int


def sha256_hex(content: bytes) -> str:
    """Return the lowercase hex digest used for every artifact key."""

    return hashlib.sha256(content).hexdigest()


def _blank_document() -> Any:
    return fitz.open()


def _finalise(document: Any) -> bytes:
    content = document.tobytes(garbage=4, deflate=True)
    document.close()
    return content


def page_count(pdf_bytes: bytes) -> int:
    """Return the page count of parseable PDF bytes or refuse."""

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as error:  # noqa: BLE001 - unparseable input is a visible refusal
        raise ConversionFailed("invalid_pdf", type(error).__name__) from error
    try:
        if document.page_count < 1:
            raise ConversionFailed("invalid_pdf", "document has no pages")
        return int(document.page_count)
    finally:
        document.close()


def is_pdf(content: bytes) -> bool:
    """Return whether the bytes carry the PDF magic header."""

    return content[:5] == b"%PDF-"


def convert_passthrough(content: bytes) -> Converted:
    """Copy a parseable PDF unchanged; never image or OCR another format."""

    if not is_pdf(content):
        raise ConversionFailed("conversion_unsupported", "source is not a PDF")
    pages = page_count(content)
    return Converted(
        pdf_bytes=content,
        page_count=pages,
        source_pages=pages,
        fallback_used=False,
        fallback_reason=None,
        blocked_resource_count=0,
    )


def _timestamp_header(policy: HtmlRenderPolicy, rendered_at: datetime) -> str:
    return policy.timestamp_header_format.format(timestamp=eat_date(rendered_at))


def _plaintext_fallback(
    archive: CommunicationArchive,
    *,
    policy: HtmlRenderPolicy,
    rendered_at: datetime,
) -> bytes:
    document = _blank_document()
    margin = policy.margin_mm * MM_TO_POINTS
    lines = [
        _timestamp_header(policy, rendered_at),
        f"Subject: {archive.subject}",
        f"From: {archive.from_addr}",
        f"To: {', '.join(archive.to_addrs)}",
        f"Date: {eat_date(archive.occurred_at)} EAT",
        "",
    ]
    lines.extend(archive.body.splitlines() or [""])
    page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
    cursor = margin
    for line in lines:
        if cursor > A4_HEIGHT - margin:
            page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            cursor = margin
        page.insert_text((margin, cursor), line[:140], fontname="helv", fontsize=9)
        cursor += 12
    return _finalise(document)


def convert_html(
    html: str,
    *,
    archive: CommunicationArchive,
    renderer: HtmlPdfRenderer,
    policy: HtmlRenderPolicy,
    rendered_at: datetime,
) -> Converted:
    """Render archived correspondence offline, with one deterministic fallback."""

    if not html.strip():
        raise ConversionFailed("invalid_archive", "archived communication body is empty")
    try:
        result = renderer.render(html, policy=policy)
    except (TimeoutError, RuntimeError, OSError) as error:
        reason = FALLBACK_REASONS.get(type(error), "html_renderer_crash")
        content = _plaintext_fallback(archive, policy=policy, rendered_at=rendered_at)
        pages = page_count(content)
        return Converted(
            pdf_bytes=content,
            page_count=pages,
            source_pages=pages,
            fallback_used=True,
            fallback_reason=reason,
            blocked_resource_count=_blocked_resources(html),
        )
    content = getattr(result, "pdf_bytes", None)
    if not isinstance(content, bytes) or not is_pdf(content):
        raise ConversionFailed("invalid_render", "renderer returned no parseable PDF")
    pages = page_count(content)
    blocked = getattr(result, "blocked_resource_count", None)
    return Converted(
        pdf_bytes=content,
        page_count=pages,
        source_pages=pages,
        fallback_used=bool(getattr(result, "fallback_used", False)),
        fallback_reason=getattr(result, "fallback_reason", None),
        blocked_resource_count=(
            int(blocked) if isinstance(blocked, int) and not isinstance(blocked, bool)
            else _blocked_resources(html)
        ),
    )


def _blocked_resources(html: str) -> int:
    return html.count("https://") + html.count("http://")


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def convert_photos(
    photos: list[tuple[str, bytes, datetime]],
    *,
    caption_format: str,
    per_page: int,
) -> tuple[bytes, list[int]]:
    """Lay photographs out ``per_page`` to an A4 page in resolved source order.

    Returns the PDF bytes and the zero-based page index each photo occupies. A
    final unpaired photo takes the first slot and the second slot stays blank.
    """

    if not photos:
        raise ConversionFailed("missing_source", "no photographs resolved")
    document = _blank_document()
    margin = 36.0
    caption_height = 18.0
    slot_height = (A4_HEIGHT - 2 * margin) / per_page
    offsets: list[int] = []
    page = None
    for index, (filename, content, received_at) in enumerate(photos):
        slot = index % per_page
        if slot == 0:
            page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
        offsets.append(document.page_count - 1)
        top = margin + slot * slot_height
        image_rect = fitz.Rect(
            margin, top, A4_WIDTH - margin, top + slot_height - caption_height
        )
        try:
            page.insert_image(image_rect, stream=content, keep_proportion=True)
        except Exception as error:  # noqa: BLE001 - an unreadable photo fails closed
            document.close()
            raise ConversionFailed("invalid_image", type(error).__name__) from error
        caption = caption_format.format(
            filename=filename, received_date=eat_date(received_at)
        )
        page.insert_htmlbox(
            fitz.Rect(
                margin,
                top + slot_height - caption_height,
                A4_WIDTH - margin,
                top + slot_height,
            ),
            f'<span style="font-family:sans-serif;font-size:9px">{_escape(caption)}</span>',
        )
    return _finalise(document), offsets


def conversion_key(
    claim_id: str,
    conversion: str,
    material: dict[str, Any],
) -> str:
    """Return the immutable, content-addressed key for one conversion result."""

    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"approval-packs/{claim_id}/converted/{conversion}/{digest}.pdf"


__all__ = [
    "A4_HEIGHT",
    "A4_WIDTH",
    "CommunicationArchive",
    "Converted",
    "ConversionFailed",
    "HtmlPdfRenderer",
    "ImmutableArtifactStore",
    "LocalImmutableStore",
    "MM_TO_POINTS",
    "conversion_key",
    "convert_html",
    "convert_passthrough",
    "convert_photos",
    "is_pdf",
    "page_count",
    "sha256_hex",
]
