"""Public PRD-07 assessment-orchestration agent boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from assessment_agent.cascade import AssessmentCascade
from assessment_agent.dispatch import AssessmentDispatch
from assessment_agent.report import AssessmentReport
from assessment_agent.selection import AssessmentSelection
from assessment_agent.trigger import AssessmentTrigger
from assessment_agent.vendors import Vendor, VendorRegistry, build_router, validate_vendors
from claim_core import Base


def _load_config(path: Path, override: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid vendor registry: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("vendor registry requires version 1")
    configured = dict(payload)
    configured.update(dict(override or {}))
    standard_fees = configured.get("standard_fees")
    shadow = configured.get("shadow")
    if (
        not isinstance(standard_fees, dict)
        or set(standard_fees) != {"physical", "desk", "reinspection"}
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in standard_fees.values()
        )
    ):
        raise ValueError("vendor registry requires the three standard fee slots")
    if (
        not isinstance(shadow, dict)
        or set(shadow)
        != {
            "purpose",
            "prompt_ref",
            "model_config_ref",
            "tier",
            "max_cost_usd",
            "claim_daily_budget_usd",
            "claim_lifetime_budget_usd",
            "platform_daily_budget_usd",
        }
        or shadow.get("tier") not in {"MODEL_LIGHT", "MODEL_HEAVY"}
        or not all(
            isinstance(shadow.get(key), str) and shadow[key]
            for key in ("purpose", "prompt_ref", "model_config_ref")
        )
        or not shadow["prompt_ref"].rpartition("@")[0]
        or not shadow["prompt_ref"].rpartition("@")[2].isdigit()
        or not all(
            isinstance(shadow.get(key), int | float)
            and not isinstance(shadow[key], bool)
            and shadow[key] > 0
            for key in (
                "max_cost_usd",
                "claim_daily_budget_usd",
                "claim_lifetime_budget_usd",
                "platform_daily_budget_usd",
            )
        )
    ):
        raise ValueError("vendor registry requires governed shadow-model configuration")
    model_path = path.parents[1] / shadow["model_config_ref"]
    try:
        model_config = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid shadow model configuration: {error}") from error
    tiers = model_config.get("tiers") if isinstance(model_config, dict) else None
    if not isinstance(tiers, dict) or shadow["tier"] not in tiers:
        raise ValueError("shadow model tier is missing from its referenced configuration")
    configured["shadow"] = {**shadow, "tiers": tiers}
    configured["vendors"] = validate_vendors(configured.get("vendors"))
    return configured


class AssessmentAgent:
    """Application-owned handle for assessment trigger and dispatch services."""

    def __init__(
        self,
        vendors: VendorRegistry,
        trigger: AssessmentTrigger,
        dispatch: AssessmentDispatch,
        report: AssessmentReport,
        cascade: AssessmentCascade,
        selection: AssessmentSelection,
    ) -> None:
        self.vendors = vendors
        self.trigger = trigger
        self.dispatch = dispatch
        self.report = report
        self.cascade = cascade
        self.selection = selection


def build_assessment_agent(
    app: Any,
    *,
    model_client: Any,
    config: dict[str, Any] | None = None,
) -> AssessmentAgent:
    """Build PRD-07 after the chase agent and shared runtime are installed."""

    if not hasattr(app.state, "chase_agent") or not hasattr(app.state, "agent_runtime"):
        raise RuntimeError("build_assessment_agent requires build_chase_agent")
    if model_client is None or not callable(getattr(model_client, "structured_call", None)):
        raise ValueError("assessment agent requires a structured model client")
    repo = Path(__file__).resolve().parents[2]
    configured = _load_config(repo / "packs/motor/vendors/vendors.yaml", config)
    Base.metadata.create_all(app.state.engine, tables=[Vendor.__table__])
    vendors = VendorRegistry(app, configured["vendors"])
    trigger = AssessmentTrigger(app, model_client, configured["shadow"])
    app.state.agent_runtime.register_step(
        "assessment.mode_shadow", "call_model", trigger.run_shadow
    )
    dispatch = AssessmentDispatch(app, vendors, trigger)
    cascade = AssessmentCascade(app)
    selection = AssessmentSelection(app, cascade)
    cascade.bind_selection(selection)
    report = AssessmentReport(app, cascade, selection)
    app.state.review_queue.service.register_resolution_validator(
        "MODE_CONFIRM", dispatch.validate_resolution
    )
    app.state.dispatcher.register_consumer("assessment_trigger", trigger.consume)
    app.state.dispatcher.register_consumer("assessment_dispatch", dispatch.consume)
    app.state.dispatcher.register_consumer("assessment_report", report.consume)
    app.state.dispatcher.register_consumer("assessment_cascade", cascade.consume)
    app.state.dispatcher.register_consumer("assessment_selection", selection.consume)
    app.include_router(build_router(app, vendors))
    agent = AssessmentAgent(vendors, trigger, dispatch, report, cascade, selection)
    app.state.assessment_agent = agent
    return agent


__all__ = ["AssessmentAgent", "build_assessment_agent"]
