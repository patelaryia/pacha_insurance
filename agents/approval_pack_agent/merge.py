"""Deterministic immutable merge of the resolved 13-item manifest (PRD-08 §8.3).

Every page of every source is copied verbatim; passthrough PDFs are never
rasterised or rewritten. With a fixed clock and identical inputs two builds are
byte-identical, because converted sources are content-addressed and reused
(register #227).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import fitz
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, NameObject
from sqlalchemy import text

from approval_pack_agent.config import (
    ApprovalPackConfig,
    ManifestItem,
    canonical_json,
    eat_date,
    utc_rfc3339,
)
from approval_pack_agent.conversion import (
    A4_HEIGHT,
    A4_WIDTH,
    CommunicationArchive,
    ConversionFailed,
    Converted,
    conversion_key,
    convert_html,
    convert_passthrough,
    convert_photos,
    sha256_hex,
)
from approval_pack_agent.resolver import ItemResolution, Readiness, ResolvedSource, _aware, _json

COVER_HEADINGS = ("Item", "Source document", "Received date", "Pages")
COLUMN_X = (40.0, 210.0, 400.0, 500.0)
LINE_HEIGHT = 15.0
COVER_TOP = 56.0
COVER_MARGIN = 40.0
PDF_PRODUCER = "Pacha claims platform"


@dataclass
class PlacedSource:
    """One converted source and the pack pages it finally occupies."""

    source: ResolvedSource
    converted: Converted
    pack_pages: tuple[int, int]


class MergeEngine:
    """Convert, concatenate, bookmark, and immutably store one pack version."""

    def __init__(
        self,
        app: Any,
        config: ApprovalPackConfig,
        *,
        renderer: Any,
        store: Any,
    ) -> None:
        self.app = app
        self.config = config
        self.renderer = renderer
        self.store = store

    # -- conversion ------------------------------------------------------------

    def _cached(
        self, claim_id: str, conversion: str, material: dict[str, Any]
    ) -> tuple[str, bytes | None]:
        key = conversion_key(claim_id, conversion, material)
        if self.store.exists(key):
            return key, self.store.get(key)
        return key, None

    def _archive(self, source: ResolvedSource) -> CommunicationArchive:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT subject, from_addr, to_addrs, occurred_at FROM communications "
                    "WHERE id = :id"
                ),
                {"id": source.id},
            ).mappings().first()
        if row is None:
            raise ConversionFailed("missing_source", source.id)
        to_addrs = _json(row["to_addrs"]) or []
        body = self.app.state.blob_store.get(source.blob_key).decode("utf-8", "replace")
        return CommunicationArchive(
            subject=str(row["subject"] or ""),
            from_addr=str(row["from_addr"] or ""),
            to_addrs=tuple(str(value) for value in to_addrs),
            occurred_at=_aware(row["occurred_at"]),
            body=body,
        )

    def _convert_single(
        self,
        claim_id: str,
        item: ManifestItem,
        source: ResolvedSource,
        rendered_at: datetime,
    ) -> Converted:
        conversion = item.conversion
        if conversion == "source_default":
            conversion = "html_to_pdf" if source.kind == "communication" else "passthrough"
        content = self.app.state.blob_store.get(source.blob_key)
        if conversion == "passthrough":
            return convert_passthrough(content)
        if conversion != "html_to_pdf":
            raise ConversionFailed("conversion_unsupported", conversion)
        # The rendered artifact carries a visible EAT timestamp header, so the
        # render time is part of its identity. Reusing a cached conversion from a
        # different render time would print a stale header (register #227).
        material = {
            "conversion": conversion,
            "policy": self.config.render_policy.digest_material(),
            "rendered_at": utc_rfc3339(rendered_at),
            "source_sha256": source.sha256,
        }
        key, cached = self._cached(claim_id, conversion, material)
        if cached is not None:
            metadata = self._conversion_metadata(key)
            return Converted(
                pdf_bytes=cached,
                page_count=int(metadata["page_count"]),
                source_pages=int(metadata["source_pages"]),
                fallback_used=bool(metadata["fallback_used"]),
                fallback_reason=metadata["fallback_reason"],
                blocked_resource_count=int(metadata["blocked_resource_count"]),
            )
        archive = self._archive(source)
        converted = convert_html(
            content.decode("utf-8", "replace"),
            archive=archive,
            renderer=self.renderer,
            policy=self.config.render_policy,
            rendered_at=rendered_at,
        )
        self.store.put_immutable(key, converted.pdf_bytes, retention=self.config.retention)
        self._store_conversion_metadata(key, converted)
        return converted

    def _conversion_metadata_key(self, key: str) -> str:
        return f"{key}.json"

    def _store_conversion_metadata(self, key: str, converted: Converted) -> None:
        payload = {
            "blocked_resource_count": converted.blocked_resource_count,
            "fallback_reason": converted.fallback_reason,
            "fallback_used": converted.fallback_used,
            "page_count": converted.page_count,
            "source_pages": converted.source_pages,
        }
        self.store.put_immutable(
            self._conversion_metadata_key(key),
            canonical_json(payload).encode("utf-8"),
            retention=self.config.retention,
        )

    def _conversion_metadata(self, key: str) -> dict[str, Any]:
        return _json(self.store.get(self._conversion_metadata_key(key)).decode("utf-8"))

    def _convert_photos(
        self, claim_id: str, sources: list[ResolvedSource]
    ) -> tuple[bytes, list[int], int]:
        material = {
            "caption_format": self.config.photo_caption_format,
            "conversion": "photos_2up",
            "per_page": self.config.photos_per_page,
            "sources": [
                [source.sha256, source.filename, utc_rfc3339(source.received_at)]
                for source in sources
            ],
        }
        key, cached = self._cached(claim_id, "photos_2up", material)
        if cached is not None:
            metadata = self._conversion_metadata(key)
            return cached, list(metadata["offsets"]), int(metadata["page_count"])
        photos = [
            (
                source.filename,
                self.app.state.blob_store.get(source.blob_key),
                source.received_at,
            )
            for source in sources
        ]
        content, offsets = convert_photos(
            photos,
            caption_format=self.config.photo_caption_format,
            per_page=self.config.photos_per_page,
        )
        pages = max(offsets) + 1
        self.store.put_immutable(key, content, retention=self.config.retention)
        self.store.put_immutable(
            self._conversion_metadata_key(key),
            canonical_json({"offsets": offsets, "page_count": pages}).encode("utf-8"),
            retention=self.config.retention,
        )
        return content, offsets, pages

    # -- cover -----------------------------------------------------------------

    def _cover(self, rows: list[tuple[str, str, str, str]], cover_pages: int) -> bytes:
        document = fitz.open()
        page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
        page.insert_text(
            (COVER_MARGIN, 40.0), "Approval pack contents", fontname="hebo", fontsize=13
        )
        cursor = COVER_TOP
        header = COVER_HEADINGS
        for column, value in zip(COLUMN_X, header, strict=True):
            page.insert_text((column, cursor), value, fontname="hebo", fontsize=9)
        cursor += LINE_HEIGHT
        for row in rows:
            if cursor > A4_HEIGHT - COVER_MARGIN:
                page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                cursor = COVER_TOP
            for column, value in zip(COLUMN_X, row, strict=True):
                page.insert_text((column, cursor), value, fontname="helv", fontsize=8)
            cursor += LINE_HEIGHT
        content = document.tobytes(garbage=4, deflate=True)
        pages = document.page_count
        document.close()
        if pages != cover_pages:
            raise ConversionFailed("cover_unstable", f"cover needs {pages} pages")
        return content

    # -- merge -----------------------------------------------------------------

    def _placements(
        self,
        claim_id: str,
        resolutions: list[ItemResolution],
        rendered_at: datetime,
        cover_pages: int,
    ) -> tuple[list[tuple[ItemResolution, list[PlacedSource]]], list[bytes]]:
        placements: list[tuple[ItemResolution, list[PlacedSource]]] = []
        segments: list[bytes] = []
        cursor = cover_pages + 1
        for resolution in resolutions:
            placed: list[PlacedSource] = []
            if resolution.item.conversion == "photos_2up":
                content, offsets, pages = self._convert_photos(claim_id, resolution.sources)
                segments.append(content)
                for source, offset in zip(resolution.sources, offsets, strict=True):
                    page = cursor + offset
                    placed.append(
                        PlacedSource(
                            source=source,
                            converted=Converted(
                                pdf_bytes=b"",
                                page_count=1,
                                source_pages=1,
                                fallback_used=False,
                                fallback_reason=None,
                                blocked_resource_count=0,
                            ),
                            pack_pages=(page, page),
                        )
                    )
                cursor += pages
                placements.append((resolution, placed))
                continue
            for source in resolution.sources:
                converted = self._convert_single(claim_id, resolution.item, source, rendered_at)
                segments.append(converted.pdf_bytes)
                placed.append(
                    PlacedSource(
                        source=source,
                        converted=converted,
                        pack_pages=(cursor, cursor + converted.page_count - 1),
                    )
                )
                cursor += converted.page_count
            placements.append((resolution, placed))
        return placements, segments

    @staticmethod
    def _pages_label(pack_pages: tuple[int, int]) -> str:
        first, last = pack_pages
        return str(first) if first == last else f"{first}-{last}"

    def _assemble(
        self,
        *,
        cover: bytes,
        segments: list[bytes],
        placements: list[tuple[ItemResolution, list[PlacedSource]]],
        filename: str,
        rendered_at: datetime,
    ) -> bytes:
        writer = PdfWriter()
        for content in (cover, *segments):
            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                writer.add_page(page)
        for resolution, placed in placements:
            if not placed:
                raise ConversionFailed("missing_source", resolution.item.id)
            writer.add_outline_item(resolution.item.label, placed[0].pack_pages[0] - 1)
        stamp = rendered_at.strftime("D:%Y%m%d%H%M%SZ")
        writer.add_metadata(
            {
                "/Producer": PDF_PRODUCER,
                "/Creator": PDF_PRODUCER,
                "/Title": filename,
                "/CreationDate": stamp,
                "/ModDate": stamp,
            }
        )
        identifier = ByteStringObject(
            bytes.fromhex(sha256_hex(filename.encode("utf-8"))[:32])
        )
        writer._ID = ArrayObject([identifier, identifier])
        writer.root_object[NameObject("/PageMode")] = NameObject("/UseOutlines")
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def build(
        self,
        *,
        claim_id: str,
        readiness: Readiness,
        version: int,
        rendered_at: datetime,
    ) -> dict[str, Any]:
        """Build one immutable version and return its exact event payload."""

        vehicle = readiness.field_rows.get("vehicle.reg")
        if vehicle is None:
            raise ConversionFailed("missing_field", "vehicle.reg")
        filename = f"All Docs merged for {vehicle.value}.pdf"
        cover_pages = 1
        for _attempt in range(4):
            placements, segments = self._placements(
                claim_id, readiness.items, rendered_at, cover_pages
            )
            rows: list[tuple[str, str, str, str]] = []
            for resolution, placed in placements:
                for index, entry in enumerate(placed):
                    rows.append(
                        (
                            resolution.item.label if index == 0 else "",
                            entry.source.filename,
                            eat_date(entry.source.received_at),
                            self._pages_label(entry.pack_pages),
                        )
                    )
            try:
                cover = self._cover(rows, cover_pages)
            except ConversionFailed as error:
                if error.code != "cover_unstable":
                    raise
                cover_pages += 1
                continue
            break
        else:  # pragma: no cover - four A4 cover pages never hold 13 rows
            raise ConversionFailed("cover_unstable", "cover page count did not settle")

        merged = self._assemble(
            cover=cover,
            segments=segments,
            placements=placements,
            filename=filename,
            rendered_at=rendered_at,
        )
        digest = sha256_hex(merged)
        blob_key = f"approval-packs/{claim_id}/merged/v{version}/{digest}.pdf"
        self.store.put_immutable(blob_key, merged, retention=self.config.retention)
        manifest = [
            {
                "item_id": resolution.item.id,
                "label": resolution.item.label,
                "bookmark": resolution.item.label,
                "sources": [
                    {
                        "kind": entry.source.kind,
                        "id": entry.source.id,
                        "filename": entry.source.filename,
                        "received_at": utc_rfc3339(entry.source.received_at),
                        "sha256": entry.source.sha256,
                        "source_pages": entry.converted.source_pages,
                        "pack_pages": [entry.pack_pages[0], entry.pack_pages[1]],
                        "fallback_used": entry.converted.fallback_used,
                        "fallback_reason": entry.converted.fallback_reason,
                        "blocked_resource_count": entry.converted.blocked_resource_count,
                    }
                    for entry in placed
                ],
            }
            for resolution, placed in placements
        ]
        return {
            "version": version,
            "filename": filename,
            "blob_key": blob_key,
            "sha256": digest,
            "rendered_at": utc_rfc3339(rendered_at),
            "object_lock_status": self.config.object_lock_status,
            "readiness_fingerprint": readiness.fingerprint,
            "manifest_version": self.config.manifest_version,
            "manifest": manifest,
        }


__all__ = ["MergeEngine", "PlacedSource"]
