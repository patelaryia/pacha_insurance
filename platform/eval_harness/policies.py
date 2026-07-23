"""Pack policy loading with a non-widenable hard-coded constitution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LEVELS = ("L0", "L1", "L2", "L3", "L4")
LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}

EXPLICIT_CEILINGS = {
    "triage.ex_gratia": "L1",
    "triage.decline_draft": "L2",
    "triage.coverage_check": "L3",
    "assessment.consistency_flag": "L2",
    "assessment.mode_confirm": "L2",
    "pack.note_draft": "L3",
}
EXPLICIT_MONEY_ADJACENT = frozenset(
    {
        "repair.authorize",
        "repair.release",
        "icon.reserve_adjust",
        "icon.assessor_payment_request",
        "icon.payment_voucher",
    }
)

# Register #273: canonical PRD-09 §9.6 capability ids are `project.<operation>`,
# so the bare-id money-adjacent set above cannot classify them. Their ceilings
# are constitution, not pack data — reserve, assessor-payment, general-payment,
# and salvage paths stay below L4, and PRD-12's three payment workflows stay at
# L2 until that gate opens. Pack policy may tighten these, never widen them.
PROJECTION_CEILINGS = {
    "project.icon.policy_read": "L4",
    "project.icon.claim_register": "L4",
    "project.icon.reserve_create": "L3",
    "project.icon.reserve_breakdown": "L3",
    "project.icon.reserve_adjust": "L3",
    "project.icon.assessor_payment_request": "L3",
    "project.icon.note_entry": "L4",
    "project.icon.claim_details_report": "L4",
    "project.icon.salvage_register": "L3",
    "project.icon.payment_voucher": "L2",
    "project.edms.general_payments": "L3",
    "project.edms.claims_workflow": "L4",
    "project.edms.attach_and_tag": "L4",
    "project.edms.claim_payment": "L2",
    "project.edms.payment_workflow": "L2",
}

FALLBACK_POLICY: dict[str, int] = {
    "l1_l2_items": 25,
    "l1_l2_grader_pass_percent": 96,
    "l2_l3_items": 50,
    "l2_l3_pass_percent": 98,
    "l3_l4_items": 100,
    "l3_l4_pass_percent": 99,
    "l3_l4_zero_critical_days": 60,
    "sampling_rate": 20,
    "sampling_floor": 5,
    "demotion_items": 20,
    "demotion_pass_percent": 95,
}


@dataclass(frozen=True)
class CapabilityPolicy:
    """A validated capability seed row."""

    capability_id: str
    max_level: str
    policy: dict[str, Any]
    initial_level: str


def constitutional_ceiling(capability_id: str) -> str:
    """Return the immutable maximum for one legal capability id."""

    if capability_id == "salvage.award" or capability_id.startswith("approval."):
        raise ValueError(f"{capability_id!r} is not a capability")
    if capability_id in EXPLICIT_CEILINGS:
        return EXPLICIT_CEILINGS[capability_id]
    if capability_id in PROJECTION_CEILINGS:
        return PROJECTION_CEILINGS[capability_id]
    if capability_id.startswith("project."):
        raise ValueError(f"{capability_id!r} is not a registered projection capability")
    if (
        capability_id.startswith(("settlement.", "salvage."))
        or capability_id in EXPLICIT_MONEY_ADJACENT
    ):
        return "L3"
    return "L4"


def _policy_values(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("policy must be a mapping")
    values = dict(raw)
    for key, value in values.items():
        if key not in FALLBACK_POLICY and key != "sampling_rate":
            raise ValueError(f"unknown autonomy policy key {key!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"autonomy policy {key!r} must be a non-negative integer")
    return values


def load_policies(path: str | Path) -> list[CapabilityPolicy]:
    """Load policy data while refusing forbidden ids and widened ceilings."""

    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid autonomy policy file: {error}") from error
    if not isinstance(payload, dict) or not set(payload).issubset(
        {"default_policy", "capabilities"}
    ):
        raise ValueError("autonomy policy file must contain capabilities")
    rows = payload.get("capabilities")
    if not isinstance(rows, list):
        raise ValueError("capabilities must be a list")
    defaults = {**FALLBACK_POLICY, **_policy_values(payload.get("default_policy"))}
    if defaults["sampling_floor"] > defaults["sampling_rate"]:
        raise ValueError("sampling_floor cannot exceed sampling_rate")
    seen: set[str] = set()
    policies: list[CapabilityPolicy] = []
    for row in rows:
        if not isinstance(row, dict) or not {"id", "max_level"} <= set(row):
            raise ValueError("capability rows require id and max_level")
        if not set(row).issubset({"id", "max_level", "policy", "initial_level"}):
            raise ValueError("capability row contains unknown keys")
        capability_id = row["id"]
        max_level = row["max_level"]
        if not isinstance(capability_id, str) or not capability_id:
            raise ValueError("capability id must be a non-empty string")
        if capability_id in seen:
            raise ValueError(f"duplicate capability {capability_id!r}")
        seen.add(capability_id)
        ceiling = constitutional_ceiling(capability_id)
        if max_level not in LEVEL_RANK:
            raise ValueError(f"unknown autonomy level {max_level!r}")
        if LEVEL_RANK[max_level] > LEVEL_RANK[ceiling]:
            raise ValueError(
                f"{capability_id!r} max_level {max_level} widens constitution {ceiling}"
            )
        initial_level = row.get("initial_level", "L1")
        if initial_level not in LEVEL_RANK or LEVEL_RANK[initial_level] > LEVEL_RANK[max_level]:
            raise ValueError(f"invalid initial level for {capability_id!r}")
        policy = {**defaults, **_policy_values(row.get("policy"))}
        if policy["sampling_floor"] > policy["sampling_rate"]:
            raise ValueError("sampling_floor cannot exceed sampling_rate")
        policies.append(
            CapabilityPolicy(
                capability_id=capability_id,
                max_level=max_level,
                policy=policy,
                initial_level=initial_level,
            )
        )
    return policies


__all__ = [
    "CapabilityPolicy",
    "LEVELS",
    "LEVEL_RANK",
    "PROJECTION_CEILINGS",
    "constitutional_ceiling",
    "load_policies",
]
