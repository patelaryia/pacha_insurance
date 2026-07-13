"""Pack-defined approval authority routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cop_runtime.errors import PackLoadError
from cop_runtime.money import Money


@dataclass(frozen=True)
class RouteResult:
    """Role and declared follow-up actions for one approval amount."""

    role: str
    side_effects: list[str]


@dataclass(frozen=True)
class AuthorityBand:
    """One inclusive authority ceiling; ``None`` is the infinity tail."""

    maximum: Money | None
    role: str
    side_effects: tuple[str, ...]


class AuthorityMatrix:
    """Validated inclusive authority bands from a pinned pack."""

    def __init__(self, bands: list[AuthorityBand]) -> None:
        self._bands = tuple(bands)

    def route(self, amount: Money) -> RouteResult:
        """Resolve a non-negative KES-cent amount to its smallest matching band."""

        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError("Routing amount must be integer KES cents")
        if amount < 0:
            raise ValueError("Routing amount cannot be negative")
        for band in self._bands:
            if band.maximum is None or amount <= band.maximum:
                return RouteResult(band.role, list(band.side_effects))
        raise RuntimeError("Validated authority matrix has no terminal band")


def load_authority_matrix(path: Path) -> AuthorityMatrix:
    """Load and validate the packet-07 implicit-contiguous band format."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackLoadError(f"Invalid authority matrix {path}: {error}") from error
    if not isinstance(raw, list) or not raw:
        raise PackLoadError("Authority matrix must be a non-empty list")

    bands: list[AuthorityBand] = []
    previous: int | None = None
    terminal_count = 0
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or not set(row).issubset(
            {"max", "role", "side_effects"}
        ):
            raise PackLoadError(f"Authority band {index} has invalid keys")
        if "max" not in row or "role" not in row:
            raise PackLoadError(f"Authority band {index} requires max and role")
        role = row["role"]
        if not isinstance(role, str) or not role.strip():
            raise PackLoadError(f"Authority band {index} requires a non-empty role")
        side_effects: Any = row.get("side_effects", [])
        if not isinstance(side_effects, list) or not all(
            isinstance(item, str) and item.strip() for item in side_effects
        ):
            raise PackLoadError(f"Authority band {index} has invalid side_effects")

        maximum = row["max"]
        if maximum is None:
            terminal_count += 1
            if index != len(raw) - 1:
                raise PackLoadError("The null authority band must be terminal")
            parsed_maximum = None
        else:
            if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
                raise PackLoadError(f"Authority band {index} max must be non-negative cents")
            if previous is not None and maximum <= previous:
                raise PackLoadError("Authority band maxima must be strictly increasing")
            previous = maximum
            parsed_maximum = Money(maximum)
        bands.append(AuthorityBand(parsed_maximum, role, tuple(side_effects)))

    if terminal_count != 1:
        raise PackLoadError("Authority matrix requires exactly one null terminal band")
    return AuthorityMatrix(bands)


__all__ = ["AuthorityMatrix", "RouteResult", "load_authority_matrix"]
