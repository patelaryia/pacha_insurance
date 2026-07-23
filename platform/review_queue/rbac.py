"""Data-defined human role and approval-band authorisation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from review_queue.contracts import ReviewContract

USER_ACTOR = re.compile(r"^user:[A-Za-z0-9]{26}$")


def _yaml(path: Path, label: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def load_roles(path: Path) -> dict[str, str]:
    raw = _yaml(path, "role config")
    roles = raw.get("roles") if isinstance(raw, dict) and "roles" in raw else raw
    if not isinstance(roles, dict):
        raise ValueError("role config must map user actors to roles")
    if not all(
        isinstance(actor, str)
        and USER_ACTOR.fullmatch(actor) is not None
        and isinstance(role, str)
        and role
        for actor, role in roles.items()
    ):
        raise ValueError("role config contains an invalid actor or role")
    return dict(roles)


def load_authority_matrix(path: Path) -> dict[str, int | None]:
    raw = _yaml(path, "authority matrix")
    if not isinstance(raw, list) or not raw:
        raise ValueError("authority matrix must be a non-empty list")
    result: dict[str, int | None] = {}
    for row in raw:
        if not isinstance(row, dict) or set(row) < {"role", "max"}:
            raise ValueError("authority matrix rows require role and max")
        role = row["role"]
        maximum = row["max"]
        if not isinstance(role, str) or not role or role in result:
            raise ValueError("authority matrix roles must be unique strings")
        if maximum is not None and (
            not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0
        ):
            raise ValueError("authority band max must be integer KES cents or null")
        result[role] = maximum
    return result


class Authorizer:
    def __init__(self, roles: dict[str, str], bands: dict[str, int | None]) -> None:
        self.roles = dict(roles)
        self.bands = dict(bands)

    def role(self, actor: str) -> str | None:
        if USER_ACTOR.fullmatch(actor) is None:
            return None
        return self.roles.get(actor)

    def resolve_role_code(
        self,
        *,
        actor: str,
        contract: ReviewContract,
        subtype: str | None,
    ) -> str | None:
        role = self.role(actor)
        if role is None or role == "auditor":
            return "FORBIDDEN_ROLE"
        if subtype == "decline_approval_required" and role != "claims_manager":
            return "FORBIDDEN_ROLE"
        if role not in contract.authorised_roles:
            return "FORBIDDEN_ROLE"
        return None

    def resolve_exact_role_code(
        self,
        *,
        actor: str,
        required_role: Any,
    ) -> str | None:
        """Authorise against one immutable required role, not a band ceiling.

        A higher role does not silently take the item: only the exact role the
        producer pinned into the review payload may see or resolve it (#248).
        """

        if not isinstance(required_role, str) or not required_role:
            return "RESOLUTION_BLOCKED_ON_INPUTS"
        role = self.role(actor)
        if role is None:
            return "FORBIDDEN_ROLE"
        if role != required_role:
            return "FORBIDDEN_BAND"
        return None

    def resolve_band_code(
        self,
        *,
        actor: str,
        contract: ReviewContract,
        band_amount: int | None,
    ) -> str | None:
        if contract.band_amount_path is None:
            return None
        if band_amount is None:
            return "RESOLUTION_BLOCKED_ON_INPUTS"
        role = self.role(actor)
        if role is None:
            return "FORBIDDEN_ROLE"
        maximum = self.bands.get(role)
        if role not in self.bands or (maximum is not None and band_amount > maximum):
            return "FORBIDDEN_BAND"
        return None


__all__ = ["Authorizer", "load_authority_matrix", "load_roles"]
