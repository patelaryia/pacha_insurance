"""Public PRD-06 document-chase agent boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from chase_agent.api import build_router
from chase_agent.checklist import ChecklistService
from chase_agent.matcher import ChaseMatcher
from chase_agent.models import ChaseChecklist, ChaseItem
from chase_agent.reminders import ReminderEngine
from claim_core import Base


def _yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid {label}: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"{label} requires version 1")
    return payload


def _load_registry(path: Path) -> dict[str, dict[str, Any]]:
    payload = _yaml(path, "checklist item registry")
    raw_items = payload.get("items")
    if not isinstance(raw_items, dict) or not raw_items:
        raise ValueError("checklist item registry requires items")
    items: dict[str, dict[str, Any]] = {}
    for item_id, raw in raw_items.items():
        if not isinstance(item_id, str) or not item_id or not isinstance(raw, dict):
            raise ValueError("checklist item registry entries must be mappings")
        kind = raw.get("kind")
        physical = raw.get("physical")
        if kind not in {"document", "physical", "field_request"} or not isinstance(
            physical, bool
        ):
            raise ValueError(f"invalid checklist item {item_id!r}")
        if (kind == "physical") != physical:
            raise ValueError(f"physical flag conflicts with kind for {item_id!r}")
        if kind == "document" and (
            not isinstance(raw.get("doc_type"), str) or not raw["doc_type"]
        ):
            raise ValueError(f"document checklist item {item_id!r} requires doc_type")
        if kind == "field_request" and (
            not isinstance(raw.get("target_path"), str) or not raw["target_path"]
        ):
            raise ValueError(f"field request {item_id!r} requires target_path")
        allowed = {"kind", "doc_type", "target_path", "physical"}
        if set(raw) - allowed:
            raise ValueError(f"checklist item {item_id!r} has unknown keys")
        items[item_id] = dict(raw)
    return items


def _load_config(path: Path, override: dict[str, Any] | None) -> dict[str, Any]:
    configured = {**_yaml(path, "chase config"), **dict(override or {})}
    cadence = configured.get("cadence_days")
    inbound = configured.get("inbound_defer")
    reasons = configured.get("reject_reasons")
    if (
        configured.get("version") != 1
        or not isinstance(cadence, list)
        or cadence != [3, 7, 12]
        or configured.get("repeat_days") != 7
        or configured.get("reminder_cap") != 6
        or inbound != {"window_hours": 24, "defer_hours": 48}
        or configured.get("cc_insured_from_reminder") != 2
        or not isinstance(reasons, dict)
        or not set(reasons) <= {
            "illegible",
            "wrong_vehicle",
            "expired",
            "wrong_document",
        }
    ):
        raise ValueError("chase config violates the PRD-06 launch contract")
    return configured


class ChaseAgent:
    """Application-owned facade exposing the deterministic Beat tick."""

    def __init__(
        self,
        checklist: ChecklistService,
        matcher: ChaseMatcher,
        reminders: ReminderEngine,
    ) -> None:
        self.checklist = checklist
        self.matcher = matcher
        self.reminders = reminders

    def tick(self, now: Any = None) -> dict[str, int]:
        return self.reminders.tick(now)


def build_chase_agent(app: Any, *, config: dict[str, Any] | None = None) -> ChaseAgent:
    """Build PRD-06 after intake and the shared agent runtime."""

    if not hasattr(app.state, "agent_runtime") or not hasattr(app.state, "intake_agent"):
        raise RuntimeError("build_chase_agent requires build_intake_agent")
    repo = Path(__file__).resolve().parents[2]
    registry = _load_registry(repo / "packs/motor/checklists/items.yaml")
    configured = _load_config(repo / "packs/motor/chase/chase.yaml", config)
    Base.metadata.create_all(
        app.state.engine,
        tables=[ChaseChecklist.__table__, ChaseItem.__table__],
    )
    checklist = ChecklistService(app, registry, configured)
    matcher = ChaseMatcher(app, checklist)
    reminders = ReminderEngine(app, checklist, configured)
    agent = ChaseAgent(checklist, matcher, reminders)
    app.state.chase_agent = agent
    app.state.dispatcher.register_consumer("chase_checklist", checklist.consume)
    app.state.dispatcher.register_consumer("chase_matcher", matcher.consume)
    app.include_router(build_router(app, checklist))
    return agent


__all__ = ["ChaseAgent", "build_chase_agent"]
