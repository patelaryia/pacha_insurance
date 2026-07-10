"""Packet-1 core field dictionary, represented as configuration data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


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
