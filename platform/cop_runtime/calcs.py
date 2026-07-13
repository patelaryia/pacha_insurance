"""Pack calculation declarations and immutable registry objects."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CalcDefinition:
    """One decorator-registered pure calculation."""

    calc_id: str
    version: str
    inputs: dict[str, str]
    optional_inputs: dict[str, str]
    function: Callable[..., Any]
    status: str
    blocked_on: tuple[str, ...]


@dataclass(frozen=True)
class CalcResult:
    """Public result of a calculation attempt."""

    calc_id: str
    calc_version: str
    pack_id: str
    pack_version: str
    status: str
    output: Any | None
    inputs: dict[str, Any]
    missing_inputs: list[str]


class CalcInputsMissing(Exception):
    """Signal that structured calculation inputs are incomplete."""

    def __init__(self, missing_inputs: list[str]) -> None:
        self.missing_inputs = missing_inputs
        super().__init__(", ".join(missing_inputs))


class CalcRegistry:
    """Read-only calculation definitions for one pack version."""

    def __init__(self, definitions: Mapping[str, CalcDefinition]) -> None:
        self._definitions = dict(definitions)

    def ids(self) -> list[str]:
        return list(self._definitions)

    def get(self, calc_id: str) -> CalcDefinition:
        try:
            return self._definitions[calc_id]
        except KeyError as error:
            raise LookupError(f"Unknown calculation id {calc_id!r}") from error


_COLLECTOR: ContextVar[list[CalcDefinition] | None] = ContextVar(
    "cop_calc_collector", default=None
)


def calc(
    calc_id: str,
    *,
    version: str,
    inputs: dict[str, str] | None = None,
    optional_inputs: dict[str, str] | None = None,
    status: str = "live",
    blocked_on: tuple[str, ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a pure pack calculation and its claim-path bindings."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        definition = CalcDefinition(
            calc_id=calc_id,
            version=version,
            inputs=dict(inputs or {}),
            optional_inputs=dict(optional_inputs or {}),
            function=function,
            status=status,
            blocked_on=tuple(blocked_on),
        )
        collector = _COLLECTOR.get()
        if collector is not None:
            collector.append(definition)
        return function

    return decorate


@contextmanager
def collect_calcs() -> Iterator[list[CalcDefinition]]:
    """Collect decorator declarations while executing one sandboxed pack module."""

    definitions: list[CalcDefinition] = []
    token = _COLLECTOR.set(definitions)
    try:
        yield definitions
    finally:
        _COLLECTOR.reset(token)


__all__ = [
    "CalcDefinition",
    "CalcInputsMissing",
    "CalcRegistry",
    "CalcResult",
    "calc",
    "collect_calcs",
]
