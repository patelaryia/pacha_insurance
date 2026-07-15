"""Fail-closed loader for review-item four-part contracts and schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
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
        self._contracts, self._subtypes = self._load(review_dir)

    @staticmethod
    def _mapping(value: Any, message: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(message)
        return value

    @classmethod
    def _load(
        cls, review_dir: Path
    ) -> tuple[dict[str, ReviewContract], dict[tuple[str, str], ReviewContract]]:
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
        subtype_contracts: dict[tuple[str, str], ReviewContract] = {}
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
            raw_subtypes = contract.get("subtypes", {})
            if not isinstance(raw_subtypes, dict):
                raise ValueError(f"{type_name} subtypes must be a mapping")
            for subtype, raw_subtype in raw_subtypes.items():
                if not isinstance(subtype, str) or not subtype:
                    raise ValueError(f"{type_name} subtype id is invalid")
                values = cls._mapping(
                    raw_subtype,
                    f"{type_name}/{subtype} subtype contract must be a mapping",
                )
                subtype_workspace = values.get("workspace_layout")
                subtype_schema_ref = values.get("resolution_schema")
                if not isinstance(subtype_workspace, str) or not subtype_workspace:
                    raise ValueError(f"{type_name}/{subtype} workspace_layout is required")
                if not isinstance(subtype_schema_ref, str) or not subtype_schema_ref.endswith("@1"):
                    raise ValueError(f"{type_name}/{subtype} resolution_schema is invalid")
                subtype_schema_path = review_dir / "schemas" / f"{subtype_schema_ref}.json"
                try:
                    subtype_schema = json.loads(
                        subtype_schema_path.read_text(encoding="utf-8")
                    )
                    Draft202012Validator.check_schema(subtype_schema)
                except (OSError, json.JSONDecodeError, SchemaError) as error:
                    raise ValueError(
                        f"invalid resolution schema {subtype_schema_ref}: {error}"
                    ) from error
                subtype_contracts[(type_name, subtype)] = ReviewContract(
                    type=type_name,
                    producing_events=tuple(producing),
                    workspace_layout=subtype_workspace,
                    resolution_actions=tuple(actions),
                    resolution_schema=subtype_schema_ref,
                    authorised_roles=tuple(roles),
                    band_amount_path=band_path,
                    schema=subtype_schema,
                )
        return loaded, subtype_contracts

    def get(self, type_name: str, subtype: str | None = None) -> ReviewContract:
        if subtype is not None and (type_name, subtype) in self._subtypes:
            return self._subtypes[(type_name, subtype)]
        return self._contracts[type_name]

    def validate(
        self,
        type_name: str,
        schema_version: str,
        payload: dict[str, Any],
        *,
        subtype: str | None = None,
    ) -> None:
        contract = self.get(type_name, subtype)
        if schema_version != contract.resolution_schema:
            raise KeyError(schema_version)
        try:
            Draft202012Validator(contract.schema).validate(payload)
        except ValidationError as error:
            raise ValueError(error.message) from error

    @property
    def types(self) -> frozenset[str]:
        return frozenset(self._contracts)

    @property
    def metadata(self) -> MappingProxyType[str, dict[str, Any]]:
        """Expose immutable console-facing contract metadata."""

        return MappingProxyType(
            {
                type_name: {
                    "workspace_layout": contract.workspace_layout,
                    "resolution_schema": contract.resolution_schema,
                    "resolution_actions": contract.resolution_actions,
                    "subtypes": {
                        subtype: {
                            "workspace_layout": subtype_contract.workspace_layout,
                            "resolution_schema": subtype_contract.resolution_schema,
                        }
                        for (owner_type, subtype), subtype_contract in self._subtypes.items()
                        if owner_type == type_name
                    },
                }
                for type_name, contract in self._contracts.items()
            }
        )


__all__ = ["ContractRegistry", "RESOLUTION_ACTIONS", "ReviewContract"]
