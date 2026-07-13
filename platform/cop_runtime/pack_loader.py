"""Interim motor-pack loader with fail-closed validation and sandbox checks."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import json_logic
import yaml
from jsonschema import Draft202012Validator

from claim_core import FieldDefinition, field_dictionary, register_dictionary_extensions
from cop_runtime.calcs import CalcDefinition, CalcRegistry, collect_calcs
from cop_runtime.rules import InputBinding, RuleDefinition, RuleRegistry

SEMVER_PATTERN = r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$"
RUNTIME_PATHS = frozenset({"runtime.routing_amount"})
RUNTIME_PATH_PREFIXES = ("runtime.latest_calc_run.",)
ALLOWED_IMPORTS = frozenset(
    {"decimal", "datetime", "cop_runtime.money", "cop_runtime.calcs"}
)
FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__"})
FORBIDDEN_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Global,
    ast.Lambda,
    ast.Nonlocal,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)

PACK_SCHEMA = {
    "type": "object",
    "required": ["id", "version", "platform_min_version", "display_strings", "config"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "version": {"type": "string", "pattern": SEMVER_PATTERN},
        "platform_min_version": {"type": "string", "pattern": SEMVER_PATTERN},
        "display_strings": {"type": "object"},
        "config": {"type": "object"},
    },
    "additionalProperties": False,
}

FIELD_SPEC_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "required": ["value_type", "pii_class"],
            "properties": {
                "value_type": {
                    "enum": ["string", "money", "date", "datetime", "bool", "enum", "object"]
                },
                "pii_class": {
                    "enum": ["none", "personal-low", "personal", "sensitive"]
                },
                "enum_values": {"type": "array", "items": {"type": "string"}},
                "allowed_source_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "blind_index": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "required": ["extend_enum_values"],
            "properties": {
                "extend_enum_values": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "additionalProperties": False,
        },
    ]
}

FIELDS_SCHEMA = {
    "type": "object",
    "required": ["version", "fields"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "fields": {
            "type": "object",
            "additionalProperties": FIELD_SPEC_SCHEMA,
        },
    },
    "additionalProperties": False,
}

INPUT_SPEC_SCHEMA = {
    "oneOf": [
        {"type": "string", "minLength": 1},
        {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "min_verification": {
                    "enum": ["extracted", "human_verified", "system_confirmed"]
                },
            },
            "additionalProperties": False,
        },
    ]
}

RULE_SCHEMA = {
    "type": "object",
    "required": [
        "id",
        "name",
        "applies_to",
        "status",
        "inputs",
        "when",
        "outcome",
        "version",
    ],
    "properties": {
        "id": {"type": "string", "pattern": r"^R-\d{2}$"},
        "name": {"type": "string", "minLength": 1},
        "applies_to": {"type": "string", "minLength": 1},
        "status": {"enum": ["live", "blocked_on_inputs"]},
        "blocked_on": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "inputs": {
            "type": "object",
            "additionalProperties": INPUT_SPEC_SCHEMA,
        },
        "when": {},
        "outcome": {"type": "object"},
        "version": {"type": "string", "pattern": SEMVER_PATTERN},
        "effective_from": {},
    },
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "blocked_on_inputs"}}},
            "then": {"required": ["blocked_on"]},
            "else": {"not": {"required": ["blocked_on"]}},
        }
    ],
}


class PackLoadError(RuntimeError):
    """A pack failed validation and was not registered."""


@dataclass(frozen=True)
class LoadedPack:
    """Fully validated runtime data for one side-by-side pack version."""

    pack_id: str
    version: str
    platform_min_version: str
    display_strings: dict[str, Any]
    config: dict[str, Any]
    rule_registry: RuleRegistry
    calc_registry: CalcRegistry
    path: Path


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackLoadError(f"Invalid YAML in {path}: {error}") from error


def _validate(schema: dict[str, Any], payload: Any, label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=str)
    if errors:
        detail = "; ".join(error.message for error in errors)
        raise PackLoadError(f"{label} failed runtime meta-schema validation: {detail}")


def peek_pack_identity(path: str | Path) -> tuple[str, str]:
    """Validate and return the identity before any registration side effect."""

    pack_path = Path(path)
    payload = _read_yaml(pack_path / "pack.yaml")
    _validate(PACK_SCHEMA, payload, str(pack_path / "pack.yaml"))
    return str(payload["id"]), str(payload["version"])


def _normalise_inputs(raw: dict[str, Any]) -> dict[str, InputBinding]:
    inputs = {}
    for alias, value in raw.items():
        if isinstance(value, str):
            inputs[alias] = InputBinding(value)
        else:
            inputs[alias] = InputBinding(
                str(value["path"]), value.get("min_verification")
            )
    return inputs


def _path_resolves(
    path: str, known_fields: set[str], config: dict[str, Any]
) -> bool:
    if path in known_fields or path in RUNTIME_PATHS:
        return True
    if path.startswith(RUNTIME_PATH_PREFIXES):
        return bool(path.removeprefix(RUNTIME_PATH_PREFIXES[0]))
    if path.startswith("pack."):
        return path.removeprefix("pack.") in config
    return False


def _validate_logic(node: Any, aliases: set[str], label: str) -> None:
    if isinstance(node, list):
        for item in node:
            _validate_logic(item, aliases, label)
        return
    if not isinstance(node, dict):
        return
    if len(node) != 1:
        raise PackLoadError(f"{label} JSONLogic objects must contain one operator")
    operator, operands = next(iter(node.items()))
    if operator not in json_logic.operations:
        raise PackLoadError(f"{label} uses unsupported JSONLogic operator {operator!r}")
    if operator == "var":
        variable = operands[0] if isinstance(operands, list) else operands
        if not isinstance(variable, str) or variable not in aliases:
            raise PackLoadError(f"{label} references undeclared input alias {variable!r}")
        return
    _validate_logic(operands, aliases, label)


def _context_fields(outcome: dict[str, Any]) -> list[str]:
    pending = []
    stack: list[Any] = [outcome]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "context_fields" and isinstance(child, list):
                    pending.extend(item for item in child if isinstance(item, str))
                else:
                    stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return pending


def _sandbox_check(path: Path) -> ast.Module:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise PackLoadError(f"Invalid calculation module {path}: {error}") from error
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name not in ALLOWED_IMPORTS for alias in node.names):
                raise PackLoadError(f"Calculation sandbox rejects import in {path}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_IMPORTS:
                raise PackLoadError(f"Calculation sandbox rejects import in {path}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                raise PackLoadError(
                    f"Calculation sandbox rejects call to {node.func.id!r} in {path}"
                )
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise PackLoadError(f"Calculation sandbox rejects name {node.id!r} in {path}")
        elif isinstance(node, ast.Attribute):
            raise PackLoadError(f"Calculation sandbox rejects attribute access in {path}")
        elif isinstance(node, FORBIDDEN_NODES):
            raise PackLoadError(
                f"Calculation sandbox rejects {type(node).__name__} in {path}"
            )
    return tree


def _load_calc_definitions(path: Path, pack_id: str, version: str) -> list[CalcDefinition]:
    _sandbox_check(path)
    digest = hashlib.sha256(f"{path.resolve()}@{pack_id}@{version}".encode()).hexdigest()
    module_name = f"_cop_pack_calcs_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PackLoadError(f"Cannot load calculation module {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        with collect_calcs() as definitions:
            spec.loader.exec_module(module)
    except Exception as error:
        raise PackLoadError(f"Calculation module {path} failed to load: {error}") from error
    return definitions


def _extension_paths(payload: dict[str, Any], core_fields: dict[str, Any]) -> set[str]:
    paths = set(core_fields)
    for field_path, definition in payload["fields"].items():
        existing = core_fields.get(field_path)
        if "extend_enum_values" in definition:
            if existing is None or existing.value_type != "enum":
                raise PackLoadError(f"Cannot extend unregistered enum field {field_path!r}")
        elif existing is not None:
            enum_values = definition.get("enum_values")
            allowed = definition.get("allowed_source_types")
            candidate = FieldDefinition(
                value_type=definition["value_type"],
                pii_class=definition["pii_class"],
                enum_values=(
                    frozenset(map(str, enum_values)) if enum_values is not None else None
                ),
                allowed_source_types=(
                    frozenset(map(str, allowed)) if allowed is not None else None
                ),
                blind_index=bool(definition.get("blind_index", False)),
            )
            if candidate != existing:
                raise PackLoadError(f"Pack may not override field {field_path!r}")
        paths.add(field_path)
    return paths


def load_pack(path: str | Path) -> LoadedPack:
    """Validate a pack completely, then register its field extensions atomically."""

    pack_path = Path(path)
    if not pack_path.is_dir():
        raise PackLoadError(f"Pack path {pack_path} is not a directory")
    yaml_payloads = {
        yaml_path: _read_yaml(yaml_path) for yaml_path in pack_path.rglob("*.yaml")
    }
    pack_file = pack_path / "pack.yaml"
    fields_file = pack_path / "fields.yaml"
    if pack_file not in yaml_payloads or fields_file not in yaml_payloads:
        raise PackLoadError("Pack requires pack.yaml and fields.yaml")
    pack_payload = yaml_payloads[pack_file]
    fields_payload = yaml_payloads[fields_file]
    _validate(PACK_SCHEMA, pack_payload, str(pack_file))
    _validate(FIELDS_SCHEMA, fields_payload, str(fields_file))

    pack_id = str(pack_payload["id"])
    version = str(pack_payload["version"])
    config = dict(pack_payload["config"])
    core_fields = field_dictionary()
    known_fields = _extension_paths(fields_payload, core_fields)

    rules: dict[str, RuleDefinition] = {}
    rule_files = sorted((pack_path / "rules").glob("*.yaml"))
    if not rule_files:
        raise PackLoadError("Pack contains no rule YAML files")
    for rule_file in rule_files:
        raw = yaml_payloads[rule_file]
        _validate(RULE_SCHEMA, raw, str(rule_file))
        rule_id = str(raw["id"])
        if rule_id in rules:
            raise PackLoadError(f"Duplicate rule id {rule_id!r}")
        inputs = _normalise_inputs(raw["inputs"])
        for binding in inputs.values():
            if not _path_resolves(binding.path, known_fields, config):
                raise PackLoadError(
                    f"Rule {rule_id} has unresolvable input path {binding.path!r}"
                )
        _validate_logic(raw["when"], set(inputs), f"Rule {rule_id}")
        pending = tuple(
            path
            for path in _context_fields(raw["outcome"])
            if path not in known_fields
        )
        rules[rule_id] = RuleDefinition(
            rule_id=rule_id,
            name=str(raw["name"]),
            applies_to=str(raw["applies_to"]),
            status=str(raw["status"]),
            blocked_on=tuple(raw.get("blocked_on", [])),
            inputs=inputs,
            when=raw["when"],
            outcome=dict(raw["outcome"]),
            version=str(raw["version"]),
            pending_field_registration=pending,
        )

    calc_file = pack_path / "calcs" / "calcs.py"
    definitions = _load_calc_definitions(calc_file, pack_id, version)
    calcs: dict[str, CalcDefinition] = {}
    for definition in definitions:
        if definition.calc_id in calcs:
            raise PackLoadError(f"Duplicate calculation id {definition.calc_id!r}")
        if definition.status not in {"live", "blocked_on_inputs"}:
            raise PackLoadError(
                f"Calculation {definition.calc_id} has invalid status {definition.status!r}"
            )
        if definition.status == "blocked_on_inputs" and not definition.blocked_on:
            raise PackLoadError(
                f"Calculation {definition.calc_id} must declare blocked_on"
            )
        for input_path in [*definition.inputs.values(), *definition.optional_inputs.values()]:
            if not _path_resolves(input_path, known_fields, config):
                raise PackLoadError(
                    f"Calculation {definition.calc_id} has unresolvable input path "
                    f"{input_path!r}"
                )
        calcs[definition.calc_id] = definition

    try:
        register_dictionary_extensions(fields_file)
    except ValueError as error:
        raise PackLoadError(f"Field extension registration failed: {error}") from error
    return LoadedPack(
        pack_id=pack_id,
        version=version,
        platform_min_version=str(pack_payload["platform_min_version"]),
        display_strings=dict(pack_payload["display_strings"]),
        config=config,
        rule_registry=RuleRegistry(rules),
        calc_registry=CalcRegistry(calcs),
        path=pack_path,
    )


__all__ = ["LoadedPack", "PackLoadError", "load_pack"]
