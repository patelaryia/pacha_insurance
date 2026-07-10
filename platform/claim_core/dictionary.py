"""Packet-1 core field dictionary, represented as configuration data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class FieldDefinition:
    """Type, PII classification, and optional enum values for one field path."""

    value_type: str
    pii_class: str
    enum_values: frozenset[str] | None = None
    allowed_source_types: frozenset[str] | None = None
    blind_index: bool = False


CORE_FIELD_DICTIONARY = {
    "policy.number": FieldDefinition("string", "none"),
    "policy.excess": FieldDefinition("money", "none"),
    "loss.date": FieldDefinition("date", "none"),
    "loss.description": FieldDefinition("string", "none"),
    "intimation.channel": FieldDefinition(
        "enum", "none", frozenset({"email", "phone", "broker", "portal"})
    ),
    "intimation.received_at": FieldDefinition("datetime", "none"),
    "parties.insured.name": FieldDefinition("string", "personal-low"),
    "parties.insured.phone": FieldDefinition("string", "personal", blind_index=True),
    "parties.insured.national_id": FieldDefinition(
        "string", "sensitive", blind_index=True
    ),
    "parties.insured.kra_pin": FieldDefinition("string", "sensitive", blind_index=True),
    "parties.insured.dl_number": FieldDefinition("string", "personal", blind_index=True),
    "parties.insured.bank_account": FieldDefinition(
        "string", "sensitive", blind_index=True
    ),
    "reserve.total": FieldDefinition("money", "none"),
    "settlement.amount": FieldDefinition("money", "none"),
    "external.icon.claim_no": FieldDefinition(
        "string", "none", allowed_source_types=frozenset({"projection_readback", "human"})
    ),
    "external.icon.salvage_no": FieldDefinition(
        "string", "none", allowed_source_types=frozenset({"projection_readback", "human"})
    ),
    "external.edms.folder_ref": FieldDefinition(
        "string", "none", allowed_source_types=frozenset({"projection_readback", "human"})
    ),
}

FIELD_DICTIONARY = dict(CORE_FIELD_DICTIONARY)
VALUE_TYPES = frozenset({"string", "money", "date", "datetime", "bool", "enum", "object"})
PII_CLASSES = frozenset({"none", "personal-low", "personal", "sensitive"})
EXTENSION_KEYS = frozenset(
    {"value_type", "pii_class", "enum_values", "allowed_source_types", "blind_index"}
)
PLAINTEXT_PII_PATHS = frozenset({"vehicle.reg"})


def requires_encryption(path: str, definition: FieldDefinition) -> bool:
    """Apply ED-6a's sole PII-at-rest plaintext exception for registration plates."""

    return definition.pii_class != "none" and path not in PLAINTEXT_PII_PATHS


def register_dictionary_extensions(path: str | Path) -> dict[str, FieldDefinition]:
    """Load a pack-owned field dictionary without permitting core overrides."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        raise ValueError("dictionary extension must contain a 'fields' mapping")
    loaded: dict[str, FieldDefinition] = {}
    for field_path, raw in raw_fields.items():
        if not isinstance(field_path, str) or not isinstance(raw, dict):
            raise ValueError("dictionary extension entries must be path mappings")
        existing = FIELD_DICTIONARY.get(field_path)
        enum_extensions = raw.get("extend_enum_values")
        if enum_extensions is not None:
            if (
                existing is None
                or existing.value_type != "enum"
                or set(raw) != {"extend_enum_values"}
                or not isinstance(enum_extensions, list)
            ):
                raise ValueError(
                    f"enum extension for {field_path!r} requires an existing enum"
                )
            definition = FieldDefinition(
                value_type=existing.value_type,
                pii_class=existing.pii_class,
                enum_values=(existing.enum_values or frozenset())
                | frozenset(map(str, enum_extensions)),
                allowed_source_types=existing.allowed_source_types,
                blind_index=existing.blind_index,
            )
            FIELD_DICTIONARY[field_path] = definition
            loaded[field_path] = definition
            continue
        if set(raw) - EXTENSION_KEYS or not {"value_type", "pii_class"} <= set(raw):
            raise ValueError(f"dictionary extension for {field_path!r} has invalid keys")
        allowed = raw.get("allowed_source_types")
        enum_values = raw.get("enum_values")
        if raw["value_type"] not in VALUE_TYPES or raw["pii_class"] not in PII_CLASSES:
            raise ValueError(f"dictionary extension for {field_path!r} has invalid values")
        definition = FieldDefinition(
            value_type=str(raw["value_type"]),
            pii_class=str(raw["pii_class"]),
            enum_values=frozenset(map(str, enum_values)) if enum_values is not None else None,
            allowed_source_types=(
                frozenset(map(str, allowed)) if allowed is not None else None
            ),
            blind_index=bool(raw.get("blind_index", False)),
        )
        if existing is not None and existing != definition:
            raise ValueError(f"dictionary extension may not override {field_path!r}")
        FIELD_DICTIONARY[field_path] = definition
        loaded[field_path] = definition
    return loaded


def field_dictionary() -> dict[str, FieldDefinition]:
    """Return a snapshot of the active core-plus-pack dictionary."""

    return dict(FIELD_DICTIONARY)


def value_matches(definition: FieldDefinition, value: Any) -> bool:
    """Return whether a JSON value satisfies a registered field definition."""

    if definition.value_type == "string":
        return isinstance(value, str)
    if definition.value_type == "money":
        return isinstance(value, int) and not isinstance(value, bool)
    if definition.value_type == "date":
        if not isinstance(value, str):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if definition.value_type == "datetime":
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None
    if definition.value_type == "bool":
        return isinstance(value, bool)
    if definition.value_type == "enum":
        return isinstance(value, str) and value in (definition.enum_values or frozenset())
    if definition.value_type == "object":
        return isinstance(value, dict)
    return False
