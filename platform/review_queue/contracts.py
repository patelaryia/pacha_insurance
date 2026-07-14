"""Fail-closed loader for review-item four-part contracts and schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from cop_runtime.contracts import REVIEW_ITEM_TYPES

RESOLUTION_ACTIONS = ["approve", "edit_approve", "reject"]


@dataclass(frozen=True)
class ReviewContract:
    type: str
    producing_events: tuple[str, ...]
    workspace_layout: str
    resolution_actions: tuple[str, ...]
    resolution_schema: str
    authorised_roles: tuple[str, ...]
    band_amount_path: str | None
    schema: dict[str, Any]


class ContractRegistry:
    """The exact closed PRD-04 type set with one pinned schema per type."""

    def __init__(self, review_dir: Path) -> None:
        self.review_dir = review_dir
        self._contracts = self._load(review_dir)

    @staticmethod
    def _mapping(value: Any, message: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(message)
        return value

    @classmethod
    def _load(cls, review_dir: Path) -> dict[str, ReviewContract]:
        try:
            raw = yaml.safe_load((review_dir / "contracts.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"invalid review contracts: {error}") from error
        root = cls._mapping(raw, "review contracts must be a mapping")
        types = cls._mapping(root.get("types", root), "review contract types must be a mapping")
        actual = set(types)
        expected = set(REVIEW_ITEM_TYPES)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"review contract type set mismatch; missing={missing}, extra={extra}")

        loaded: dict[str, ReviewContract] = {}
        for type_name in sorted(expected):
            contract = cls._mapping(types[type_name], f"{type_name} contract must be a mapping")
            producing = contract.get("producing_events")
            workspace = contract.get("workspace_layout")
            actions = contract.get("resolution_actions")
            schema_ref = contract.get("resolution_schema")
            roles = contract.get("authorised_roles")
            band_path = contract.get("band_amount_path")
            if not (
                isinstance(producing, list)
                and producing
                and all(isinstance(item, str) and item for item in producing)
            ):
                raise ValueError(f"{type_name} producing_events must be non-empty strings")
            if not isinstance(workspace, str) or not workspace:
                raise ValueError(f"{type_name} workspace_layout is required")
            if actions != RESOLUTION_ACTIONS:
                raise ValueError(f"{type_name} resolution_actions must be the closed action list")
            if schema_ref != f"{type_name}@1":
                raise ValueError(f"{type_name} resolution_schema must be {type_name}@1")
            if not (
                isinstance(roles, list)
                and roles
                and all(isinstance(role, str) and role for role in roles)
            ):
                raise ValueError(f"{type_name} authorised_roles must be non-empty strings")
            if band_path is not None and (not isinstance(band_path, str) or not band_path):
                raise ValueError(f"{type_name} band_amount_path must be a field path")

            schema_path = review_dir / "schemas" / f"{schema_ref}.json"
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
            except (OSError, json.JSONDecodeError, SchemaError) as error:
                raise ValueError(f"invalid resolution schema {schema_ref}: {error}") from error
            loaded[type_name] = ReviewContract(
                type=type_name,
                producing_events=tuple(producing),
                workspace_layout=workspace,
                resolution_actions=tuple(actions),
                resolution_schema=schema_ref,
                authorised_roles=tuple(roles),
                band_amount_path=band_path,
                schema=schema,
            )
        return loaded

    def get(self, type_name: str) -> ReviewContract:
        return self._contracts[type_name]

    def validate(self, type_name: str, schema_version: str, payload: dict[str, Any]) -> None:
        contract = self.get(type_name)
        if schema_version != contract.resolution_schema:
            raise KeyError(schema_version)
        try:
            Draft202012Validator(contract.schema).validate(payload)
        except ValidationError as error:
            raise ValueError(error.message) from error

    @property
    def types(self) -> frozenset[str]:
        return frozenset(self._contracts)


__all__ = ["ContractRegistry", "RESOLUTION_ACTIONS", "ReviewContract"]
