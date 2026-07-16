"""Public PRD-05 slice-1 intake router and assigner boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from intake_agent.assigner import ClaimAssigner
from intake_agent.classifier import MailboxClassifier
from intake_agent.flow import IntakeFlow, TerminalMoneyConsumer
from intake_agent.router import EmailRouter
from intake_agent.triage import ModeATriage


class IntakeAgent:
    """Application-owned handle exposing the two deterministic consumers."""

    def __init__(
        self,
        router: EmailRouter,
        assigner: ClaimAssigner,
        flow: IntakeFlow,
        terminal_money: TerminalMoneyConsumer,
    ) -> None:
        self.router = router
        self.assigner = assigner
        self.flow = flow
        self.terminal_money = terminal_money


def _load_config(path: Path, override: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid intake agent config: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("intake agent config requires version 1")
    configured = {**payload, **dict(override or {})}
    self_addresses = configured.get("self_addresses")
    sample_rate = configured.get("archive_sample_rate")
    classifier = configured.get("classifier")
    checklist = configured.get("checklist_base_items")
    if (
        not isinstance(self_addresses, list)
        or not all(isinstance(value, str) and value for value in self_addresses)
        or not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or not 0 <= sample_rate <= 100
        or not isinstance(classifier, dict)
        or not isinstance(checklist, list)
        or not all(isinstance(value, str) and value for value in checklist)
        or len(checklist) != len(set(checklist))
    ):
        raise ValueError("intake agent config contains invalid values")
    thresholds = classifier.get("thresholds")
    if (
        not isinstance(thresholds, dict)
        or thresholds.get("new_intimation") != 0.85
        or thresholds.get("not_a_claim") != 0.95
    ):
        raise ValueError("classifier boundaries must match PRD-05 §5.2")
    return configured


def _load_checklist_registry(path: Path, base_items: list[str]) -> dict[str, dict[str, Any]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid checklist item registry: {error}") from error
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(items, dict):
        raise ValueError("checklist item registry requires version 1 and items")
    missing = set(base_items) - set(items)
    if missing:
        raise ValueError(f"checklist item registry omits base items: {sorted(missing)}")
    for item_id in base_items:
        row = items[item_id]
        if not isinstance(row, dict) or row.get("kind") != "document":
            raise ValueError(f"base checklist item {item_id!r} must be a document")
        if not isinstance(row.get("doc_type"), str) or not row["doc_type"]:
            raise ValueError(f"base checklist item {item_id!r} requires doc_type")
    return {str(key): dict(value) for key, value in items.items()}


def build_intake_agent(
    app: Any,
    *,
    classifier: Any = None,
    officers: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> IntakeAgent:
    """Build router/assigner consumers after the shared agent runtime."""

    if not hasattr(app.state, "agent_runtime"):
        raise RuntimeError("build_intake_agent requires build_agent_runtime")
    repo = Path(__file__).resolve().parents[2]
    configured = _load_config(repo / "packs" / "motor" / "intake" / "intake.yaml", config)
    configured["checklist_registry"] = _load_checklist_registry(
        repo / "packs" / "motor" / "checklists" / "items.yaml",
        configured["checklist_base_items"],
    )
    if officers is None:
        roles = dict(app.state.review_queue.roles)
        officer_pool = [actor for actor, role in roles.items() if role == "claims_officer"]
    else:
        officer_pool = list(officers)
    effective_classifier = classifier or MailboxClassifier(app, configured)
    router = EmailRouter(app, effective_classifier, configured)
    assigner = ClaimAssigner(app, officer_pool)
    triage = ModeATriage(app)
    flow = IntakeFlow(app, configured, triage)
    terminal_money = TerminalMoneyConsumer(
        app, configured.get("money_relevant_doc_types")
    )
    handle = IntakeAgent(router, assigner, flow, terminal_money)
    app.state.intake_agent = handle
    app.state.dispatcher.register_consumer("intake_router", router.consume)
    app.state.dispatcher.register_consumer("intake_assigner", assigner.consume)
    app.state.dispatcher.register_consumer("intake_flow", flow.consume)
    app.state.dispatcher.register_consumer(
        "intake_terminal_money", terminal_money.consume
    )
    return handle


__all__ = ["IntakeAgent", "build_intake_agent"]
