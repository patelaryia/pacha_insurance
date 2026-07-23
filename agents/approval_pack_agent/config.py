"""Pack-owned approval-pack configuration, validated at startup.

Everything the merge engine, the T-01 slot map, the render policy, and the
commentary model call need is pack data (guide §4, config over code). A
malformed or widened pack refuses to install rather than defaulting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from doc_intel import registered_doc_types

EAT = timezone(timedelta(hours=3), "EAT")

ITEM_KEYS = frozenset(
    {
        "id",
        "order",
        "label",
        "source_kinds",
        "selector",
        "repeatable",
        "conversion",
        "required",
        "waivable",
    }
)
SOURCE_KINDS = frozenset({"document", "communication", "projection_readback", "upload"})
SELECTORS = frozenset(
    {"explicit", "auto_doc_type", "selected_assessor_report", "projection_or_upload"}
)
CONVERSIONS = frozenset({"passthrough", "html_to_pdf", "photos_2up", "source_default"})
UPLOAD_ITEM_IDS = ("assessor_payment_request", "claim_details_report")
SLOT_STATUSES = frozenset({"active", "blocked_on_inputs", "pending_capture"})


class PackConfigError(ValueError):
    """The approval-pack configuration is invalid; installation must refuse."""


def canonical_json(value: Any) -> str:
    """Return binding sorted, compact UTF-8 JSON text."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def money_display(cents: int) -> str:
    """Format integer KES cents; no value ever round-trips through a float."""

    if not isinstance(cents, int) or isinstance(cents, bool):
        raise TypeError("money_display requires integer KES cents")
    shillings, remainder = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    if remainder == 0:
        return f"KES {sign}{shillings:,}"
    return f"KES {sign}{shillings:,}.{remainder:02d}"


def eat_date(value: datetime) -> str:
    """Render a stored UTC instant as its East African Time calendar date."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(EAT).date().isoformat()


def eat_timestamp(value: datetime) -> str:
    """Render a stored UTC instant as its East African Time wall clock."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(EAT).strftime("%Y-%m-%d %H:%M")


def utc_rfc3339(value: datetime) -> str:
    """Render a stored instant as a UTC RFC3339 string."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ManifestItem:
    """One validated manifest row."""

    id: str
    order: int
    label: str
    source_kinds: tuple[str, ...]
    selector: str
    repeatable: bool
    conversion: str
    required: bool
    waivable: bool
    doc_types: tuple[str, ...]


@dataclass(frozen=True)
class HtmlRenderPolicy:
    """The binding offline rendering contract handed to every renderer call."""

    network_enabled: bool
    allowed_schemes: tuple[str, ...]
    page: str
    orientation: str
    margin_mm: int
    viewport_width_px: int
    viewport_height_px: int
    print_css_enabled: bool
    timeout_seconds: int
    render_time_zone: str
    timestamp_header_format: str
    timestamp_time_zone: str

    def digest_material(self) -> dict[str, Any]:
        """Return the policy fields that make a converted artifact reusable."""

        return {
            "allowed_schemes": list(self.allowed_schemes),
            "margin_mm": self.margin_mm,
            "network_enabled": self.network_enabled,
            "orientation": self.orientation,
            "page": self.page,
            "print_css_enabled": self.print_css_enabled,
            "timeout_seconds": self.timeout_seconds,
            "timestamp_header_format": self.timestamp_header_format,
            "timestamp_time_zone": self.timestamp_time_zone,
            "viewport_height_px": self.viewport_height_px,
            "viewport_width_px": self.viewport_width_px,
        }


@dataclass(frozen=True)
class ApprovalPackConfig:
    """The complete validated pack surface for one installation."""

    manifest_version: int
    items: tuple[ManifestItem, ...]
    note: dict[str, Any]
    commentary: dict[str, Any]
    render_policy: HtmlRenderPolicy
    photo_caption_format: str
    photos_per_page: int
    retention: str
    object_lock_status: str

    def item(self, item_id: str) -> ManifestItem:
        for row in self.items:
            if row.id == item_id:
                return row
        raise LookupError(f"unknown manifest item {item_id!r}")


def _yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackConfigError(f"invalid {label}: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise PackConfigError(f"{label} requires version 1")
    return payload


def _load_manifest(path: Path) -> tuple[int, tuple[ManifestItem, ...]]:
    payload = _yaml(path, "approval pack manifest")
    rows = payload.get("items")
    if set(payload) != {"version", "items"} or not isinstance(rows, list) or not rows:
        raise PackConfigError("approval pack manifest requires version and items")
    known_doc_types = set(registered_doc_types())
    items: list[ManifestItem] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise PackConfigError("manifest items must be mappings")
        allowed = ITEM_KEYS | ({"doc_types"} if raw.get("selector") == "auto_doc_type" else set())
        if set(raw) - allowed or not ITEM_KEYS <= set(raw):
            raise PackConfigError(f"manifest item {raw.get('id')!r} has invalid keys")
        item_id = raw["id"]
        order = raw["order"]
        label = raw["label"]
        source_kinds = raw["source_kinds"]
        selector = raw["selector"]
        conversion = raw["conversion"]
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in seen_ids
            or not isinstance(order, int)
            or isinstance(order, bool)
            or order in seen_orders
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(source_kinds, list)
            or not source_kinds
            or not set(source_kinds) <= SOURCE_KINDS
            or selector not in SELECTORS
            or conversion not in CONVERSIONS
            or not isinstance(raw["repeatable"], bool)
            or not isinstance(raw["required"], bool)
            or not isinstance(raw["waivable"], bool)
        ):
            raise PackConfigError(f"manifest item {item_id!r} has invalid values")
        doc_types = tuple(raw.get("doc_types", ()))
        if selector == "auto_doc_type":
            if not doc_types or not all(isinstance(value, str) for value in doc_types):
                raise PackConfigError(f"manifest item {item_id!r} requires doc_types")
            unknown = sorted(set(doc_types) - known_doc_types)
            if unknown:
                raise PackConfigError(
                    f"manifest item {item_id!r} references unregistered doc types {unknown}"
                )
        if selector == "projection_or_upload" and item_id not in UPLOAD_ITEM_IDS:
            raise PackConfigError(f"manifest item {item_id!r} may not use projection_or_upload")
        seen_ids.add(item_id)
        seen_orders.add(order)
        items.append(
            ManifestItem(
                id=item_id,
                order=order,
                label=label,
                source_kinds=tuple(source_kinds),
                selector=selector,
                repeatable=bool(raw["repeatable"]),
                conversion=conversion,
                required=bool(raw["required"]),
                waivable=bool(raw["waivable"]),
                doc_types=doc_types,
            )
        )
    items.sort(key=lambda row: row.order)
    if [row.order for row in items] != list(range(1, len(items) + 1)):
        raise PackConfigError("manifest orders must be contiguous from 1")
    return int(payload["version"]), tuple(items)


def _load_note(path: Path) -> dict[str, Any]:
    payload = _yaml(path, "approval note slot map")
    computed = payload.get("computed_slots")
    verification = payload.get("verification_slots")
    commentary = payload.get("commentary_slots")
    slots = payload.get("slots")
    if (
        not isinstance(computed, list)
        or not isinstance(verification, list)
        or not isinstance(commentary, list)
        or not isinstance(slots, dict)
        or set(slots) != set(computed)
        or len(set(computed)) != len(computed)
        or not isinstance(payload.get("verification"), dict)
        or set(payload["verification"]) != set(verification)
        or not isinstance(payload.get("commentary"), dict)
        or set(payload["commentary"]) != set(commentary)
        or not isinstance(payload.get("blocked_marker"), str)
    ):
        raise PackConfigError("approval note slot map is incomplete")
    for slot_id, raw in slots.items():
        status = raw.get("status") if isinstance(raw, dict) else None
        if status not in SLOT_STATUSES:
            raise PackConfigError(f"note slot {slot_id!r} has an invalid status")
        if status == "active" and (
            raw.get("source") != "claim_field" or not isinstance(raw.get("field_path"), str)
        ):
            raise PackConfigError(f"active note slot {slot_id!r} requires a claim field")
        if status != "active" and "value" in raw:
            raise PackConfigError(f"unresolved note slot {slot_id!r} must not carry a value")
        if status == "pending_capture" and raw.get("placeholder") != "PENDING CAPTURE":
            raise PackConfigError(f"note slot {slot_id!r} must use the mandated placeholder")
        if status != "active" and not isinstance(raw.get("blocker"), str):
            raise PackConfigError(f"note slot {slot_id!r} requires a register reference")
    return payload


def _load_commentary(path: Path) -> dict[str, Any]:
    payload = _yaml(path, "approval note commentary config")
    prompt_ref = payload.get("prompt_ref")
    sections = payload.get("sections")
    if (
        payload.get("task") != "pack_note_commentary"
        or payload.get("tier") not in {"MODEL_LIGHT", "MODEL_HEAVY"}
        or not isinstance(prompt_ref, str)
        or "@" not in prompt_ref
        or not isinstance(sections, list)
        or not sections
        or payload.get("locale") != "en-GB"
        or not isinstance(payload.get("incident_summary_max_words"), int)
        or isinstance(payload.get("incident_summary_max_words"), bool)
        or not isinstance(payload.get("forbidden_terms"), list)
        or not isinstance(payload.get("american_spellings"), dict)
        or not all(
            isinstance(payload.get(key), int | float)
            and not isinstance(payload[key], bool)
            and payload[key] > 0
            for key in (
                "max_input_tokens",
                "max_output_tokens",
                "max_cost_usd",
                "claim_daily_budget_usd",
                "claim_lifetime_budget_usd",
                "platform_daily_budget_usd",
            )
        )
    ):
        raise PackConfigError("approval note commentary config is invalid")
    return payload


def _load_render(path: Path) -> tuple[HtmlRenderPolicy, dict[str, Any]]:
    payload = _yaml(path, "approval pack render policy")
    html = payload.get("html")
    photos = payload.get("photos")
    store = payload.get("store")
    if not isinstance(html, dict) or not isinstance(photos, dict) or not isinstance(store, dict):
        raise PackConfigError("render policy requires html, photos, and store sections")
    if (
        html.get("network_enabled") is not False
        or sorted(html.get("allowed_schemes") or []) != ["cid", "data"]
        or html.get("page") != "A4"
        or html.get("orientation") != "portrait"
        or not isinstance(html.get("margin_mm"), int)
        or html.get("print_css_enabled") is not True
        or not isinstance(html.get("timeout_seconds"), int)
        or html.get("render_time_zone") != "UTC"
        or not isinstance(html.get("timestamp_header_format"), str)
    ):
        raise PackConfigError("render policy violates the binding offline contract")
    if photos.get("per_page") != 2 or not isinstance(photos.get("caption_format"), str):
        raise PackConfigError("photo policy must be 2-up with a captured caption format")
    if not isinstance(store.get("retention"), str) or store.get("object_lock_status") not in {
        "local_write_once",
        "s3_object_lock",
    }:
        raise PackConfigError("immutable store policy is invalid")
    policy = HtmlRenderPolicy(
        network_enabled=False,
        allowed_schemes=("data", "cid"),
        page=str(html["page"]),
        orientation=str(html["orientation"]),
        margin_mm=int(html["margin_mm"]),
        viewport_width_px=int(html["viewport_width_px"]),
        viewport_height_px=int(html["viewport_height_px"]),
        print_css_enabled=True,
        timeout_seconds=int(html["timeout_seconds"]),
        render_time_zone=str(html["render_time_zone"]),
        timestamp_header_format=str(html["timestamp_header_format"]),
        timestamp_time_zone=str(html.get("timestamp_time_zone", "Africa/Nairobi")),
    )
    return policy, {"photos": photos, "store": store}


def load_config(pack_root: Path, override: dict[str, Any] | None = None) -> ApprovalPackConfig:
    """Load and validate every approval-pack file for one motor pack root."""

    directory = pack_root / "approval_pack"
    manifest_version, items = _load_manifest(directory / "manifest.yaml")
    note = _load_note(directory / "note.yaml")
    commentary = {**_load_commentary(directory / "commentary.yaml"), **dict(override or {})}
    policy, extras = _load_render(directory / "render.yaml")
    if list(commentary["sections"]) != list(note["commentary_slots"]):
        raise PackConfigError("commentary sections must match the note commentary slots")
    return ApprovalPackConfig(
        manifest_version=manifest_version,
        items=items,
        note=note,
        commentary=commentary,
        render_policy=policy,
        photo_caption_format=str(extras["photos"]["caption_format"]),
        photos_per_page=int(extras["photos"]["per_page"]),
        retention=str(extras["store"]["retention"]),
        object_lock_status=str(extras["store"]["object_lock_status"]),
    )


__all__ = [
    "EAT",
    "ApprovalPackConfig",
    "HtmlRenderPolicy",
    "ManifestItem",
    "PackConfigError",
    "canonical_json",
    "eat_date",
    "eat_timestamp",
    "load_config",
    "money_display",
    "utc_rfc3339",
]
