"""Immutable rule declarations and JSONLogic evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from json_logic import jsonLogic


@dataclass(frozen=True)
class InputBinding:
    """One named rule or calculation input."""

    path: str
    min_verification: str | None = None


@dataclass(frozen=True)
class RuleDefinition:
    """One compiled pack rule."""

    rule_id: str
    name: str
    applies_to: str
    status: str
    blocked_on: tuple[str, ...]
    inputs: dict[str, InputBinding]
    when: Any
    outcome: dict[str, Any]
    version: str
    pending_field_registration: tuple[str, ...]


@dataclass(frozen=True)
class RuleResult:
    """Public result of a rule evaluation attempt."""

    claim_id: str
    rule_run_id: str
    rule_id: str
    rule_version: str
    pack_id: str
    pack_version: str
    status: str
    fired: bool | None
    outcome: dict[str, Any] | None
    inputs_snapshot: dict[str, Any]
    missing_inputs: list[str]


class RuleRegistry:
    """Read-only rule definitions for one pack version."""

    def __init__(self, definitions: dict[str, RuleDefinition]) -> None:
        self._definitions = dict(definitions)

    def ids(self) -> list[str]:
        return list(self._definitions)

    def get(self, rule_id: str) -> RuleDefinition:
        try:
            return self._definitions[rule_id]
        except KeyError as error:
            raise LookupError(f"Unknown rule id {rule_id!r}") from error


def evaluate_logic(definition: RuleDefinition, inputs: dict[str, Any]) -> bool:
    """Evaluate one already-validated JSONLogic expression."""

    return bool(jsonLogic(definition.when, inputs))


__all__ = [
    "InputBinding",
    "RuleDefinition",
    "RuleRegistry",
    "RuleResult",
    "evaluate_logic",
]
