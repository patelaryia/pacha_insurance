"""Pack-registered extraction schemas and deterministic prompt generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELD_KEYS = frozenset({"type", "required", "validator", "pii_class"})
FIELD_TYPES = frozenset(
    {"string", "money", "date", "datetime", "bool", "enum", "object", "array", "integer", "number"}
)
PII_CLASSES = frozenset({"none", "personal-low", "personal", "sensitive"})
VALIDATOR_NAMES = frozenset(
    {
        "kenya_reg",
        "kra_pin",
        "date_past",
        "money_kes",
        "sum_check",
        "licence_no",
        "phone_ke",
        "not_applicable",
    }
)


class SchemaRegistry:
    """Strict registry for one or more pack-owned document schemas."""

    def __init__(self, field_dictionary: dict[str, Any]) -> None:
        self._field_dictionary = field_dictionary
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(self, schema: dict[str, Any]) -> None:
        doc_type = schema.get("doc_type")
        fields = schema.get("fields")
        if not isinstance(doc_type, str) or not isinstance(fields, dict):
            raise ValueError("schema requires doc_type and fields")
        if doc_type in self._schemas:
            raise ValueError(f"document schema {doc_type!r} is already registered")
        for name, definition in fields.items():
            if not isinstance(name, str) or not isinstance(definition, dict):
                raise ValueError("schema fields must be named mappings")
            missing = REQUIRED_FIELD_KEYS - definition.keys()
            if missing:
                raise ValueError(f"field {name!r} is missing {sorted(missing)}")
            if definition["type"] not in FIELD_TYPES:
                raise ValueError(f"field {name!r} has an unknown type")
            if not isinstance(definition["required"], bool):
                raise ValueError(f"field {name!r} required must be boolean")
            if definition["validator"] not in VALIDATOR_NAMES:
                raise ValueError(f"field {name!r} has an unknown validator")
            if definition["pii_class"] not in PII_CLASSES:
                raise ValueError(f"field {name!r} has an unknown pii_class")
            threshold = definition.get("confidence_threshold")
            if threshold is not None and (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or not 0 <= threshold <= 1
            ):
                raise ValueError(f"field {name!r} has an invalid confidence threshold")
            if "always_review" in definition and not isinstance(
                definition["always_review"], bool
            ):
                raise ValueError(f"field {name!r} always_review must be boolean")
            target = definition.get("target_path")
            if target is not None and target not in self._field_dictionary:
                raise ValueError(f"unregistered target_path {target!r}")
            if target is not None:
                target_definition = self._field_dictionary[target]
                if definition["type"] != target_definition.value_type:
                    raise ValueError(f"field {name!r} type does not match {target!r}")
                if definition["pii_class"] != target_definition.pii_class:
                    raise ValueError(f"field {name!r} pii_class does not match {target!r}")
        self._schemas[doc_type] = schema

    def load_directory(self, path: str | Path) -> None:
        for schema_path in sorted(Path(path).glob("*.yaml")):
            payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"schema {schema_path} must be a mapping")
            self.register(payload)

    def doc_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))

    def schema_for(self, doc_type: str) -> dict[str, Any]:
        try:
            return self._schemas[doc_type]
        except KeyError as error:
            raise KeyError(f"unknown document type {doc_type!r}") from error

    def prompt_for(self, doc_type: str) -> str:
        schema = self.schema_for(doc_type)
        lines = [
            f"Extract the registered fields for document type: {doc_type}.",
            "For every field return value, anchor_text (verbatim, at most 120 chars), "
            "page, and confidence.",
        ]
        for name, definition in schema["fields"].items():
            description = definition.get("description", "")
            example = definition.get("example", "")
            lines.append(
                f"- {name}: type={definition['type']}; required={definition['required']}; "
                f"description={description}; example={example}"
            )
        return "\n".join(lines)

    def extraction_output_schema(self, doc_type: str) -> dict[str, Any]:
        registered_fields = self.schema_for(doc_type)["fields"]
        names = list(registered_fields)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["fields"],
            "additionalProperties": False,
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "name",
                            "value",
                            "anchor_text",
                            "page",
                            "confidence",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "enum": names},
                            "value": {},
                            "anchor_text": {"type": "string", "maxLength": 120},
                            "page": {"type": "integer", "minimum": 1},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                    },
                }
            },
            "allOf": [
                {
                    "properties": {
                        "fields": {
                            "contains": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {"name": {"const": name}},
                            },
                            "minContains": 1 if definition["required"] else 0,
                            "maxContains": 1,
                        }
                    }
                }
                for name, definition in registered_fields.items()
            ],
        }
