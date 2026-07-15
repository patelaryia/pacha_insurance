"""Public PRD-05 slice-1 intake router and assigner boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from intake_agent.assigner import ClaimAssigner
from intake_agent.classifier import MailboxClassifier
from intake_agent.router import EmailRouter


class IntakeAgent:
    """Application-owned handle exposing the two deterministic consumers."""

    def __init__(self, router: EmailRouter, assigner: ClaimAssigner) -> None:
        self.router = router
        self.assigner = assigner


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
    if (
        not isinstance(self_addresses, list)
        or not all(isinstance(value, str) and value for value in self_addresses)
        or not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or not 0 <= sample_rate <= 100
        or not isinstance(classifier, dict)
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
    if officers is None:
        roles = dict(app.state.review_queue.roles)
        officer_pool = [actor for actor, role in roles.items() if role == "claims_officer"]
    else:
        officer_pool = list(officers)
    effective_classifier = classifier or MailboxClassifier(app, configured)
    router = EmailRouter(app, effective_classifier, configured)
    assigner = ClaimAssigner(app, officer_pool)
    handle = IntakeAgent(router, assigner)
    app.state.intake_agent = handle
    app.state.dispatcher.register_consumer("intake_router", router.consume)
    app.state.dispatcher.register_consumer("intake_assigner", assigner.consume)
    return handle


__all__ = ["IntakeAgent", "build_intake_agent"]
