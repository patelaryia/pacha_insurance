"""Executable, role-closed Temporal Worker process for ECS/Fargate.

The application factory supplies domain dependencies only. Registration is
owned here and is an explicit closed list, so a deployment cannot widen a
Worker's authority by returning arbitrary Workflows or Activities.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.projection import AgentRunProjection
from approval_pack_agent.activities import (
    ApprovalPackActivities,
    approval_activity_registrations,
)
from approval_pack_agent.workflows import APPROVAL_PACK_WORKFLOWS
from assessment_agent.activities import (
    AssessmentActivities,
    assessment_control_activity_registrations,
    assessment_effect_activity_registrations,
)
from assessment_agent.workflows import ASSESSMENT_WORKFLOWS
from chase_agent.activities import chase_activity_registrations
from intake_agent.activities import (
    IntakeActivities,
    intake_control_activity_registrations,
    intake_effect_activity_registrations,
)
from intake_agent.workflows import INTAKE_WORKFLOWS
from orchestration.activities import (
    TEMPORAL_INTENT_CONSUMER,
    AgentRunActivities,
    RecurringActivities,
    SystemActivities,
    control_activity_registrations,
    ledger_activity_registrations,
    recurring_activity_registrations,
)
from orchestration.chase_workflow import DocumentChaseWorkflow
from orchestration.client import build_temporal_client
from orchestration.config import TemporalConfig
from orchestration.errors import ConfigurationError
from orchestration.observability import configure_control_logging
from orchestration.schedules import RECURRING_WORKFLOWS
from orchestration.starter import (
    TEMPORAL_INTENT_MAPPINGS,
    TemporalIntentConsumer,
    TemporalStarter,
)
from orchestration.telemetry import build_runtime_telemetry
from orchestration.worker import build_worker
from orchestration.workflows import SYSTEM_WORKFLOWS
from projection_agent.activities import (
    ProjectionActivities,
    projection_control_activity_registrations,
    projection_effect_activity_registrations,
)
from projection_agent.workflows import PROJECTION_WORKFLOWS

_FACTORY_ENV = "PACHA_WORKER_DEPENDENCIES_FACTORY"


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    """Infrastructure-built domain objects; never a registration surface."""

    app: Any
    docintel_engine: Any | None = None


class DependenciesFactory(Protocol):
    def __call__(self, *, role: str, build_id: str) -> WorkerDependencies: ...


def _load_factory(reference: str) -> DependenciesFactory:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ConfigurationError(f"{_FACTORY_ENV} must use module:attribute")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ConfigurationError(f"{_FACTORY_ENV} does not resolve to a callable")
    return factory


def _dependencies(config: TemporalConfig) -> WorkerDependencies:
    reference = os.environ.get(_FACTORY_ENV, "").strip()
    if not reference:
        raise ConfigurationError(f"{_FACTORY_ENV} is required")
    value = _load_factory(reference)(role=config.worker_role or "", build_id=config.build_id)
    if not isinstance(value, WorkerDependencies):
        raise ConfigurationError(
            f"{_FACTORY_ENV} must return orchestration.runtime.WorkerDependencies"
        )
    if not hasattr(value.app, "state"):
        raise ConfigurationError("Worker dependencies require an application state")
    return value


def _install_temporal_bridge(app: Any, starter: TemporalStarter) -> None:
    dispatcher = getattr(app.state, "dispatcher", None)
    if dispatcher is None:
        raise ConfigurationError("Worker application has no dispatcher")
    if TEMPORAL_INTENT_CONSUMER not in dispatcher.consumer_names:
        dispatcher.register_consumer(
            TEMPORAL_INTENT_CONSUMER,
            TemporalIntentConsumer(starter, TEMPORAL_INTENT_MAPPINGS),
        )


def _control_bindings(app: Any, build_id: str) -> tuple[tuple[type, ...], tuple[Any, ...]]:
    for dependency in (
        "agent_runtime",
        "chase_agent",
        "assessment_agent",
        "approval_pack_agent",
        "projection_agent",
        "notify",
        "graph_integration",
        "eval_harness",
    ):
        if not hasattr(app.state, dependency):
            raise ConfigurationError(f"control Worker dependency is missing: {dependency}")

    system = SystemActivities(app)
    agent_runs = AgentRunActivities(
        AgentRunProjection(app), worker_build_id=build_id
    )
    recurring = RecurringActivities(app)
    chase = app.state.chase_agent.temporal_activities(worker_build_id=build_id)
    intake = IntakeActivities(app)
    assessment = AssessmentActivities(app)
    approval = ApprovalPackActivities(app)
    projection = ProjectionActivities(app)
    workflows = (
        *SYSTEM_WORKFLOWS,
        *RECURRING_WORKFLOWS,
        DocumentChaseWorkflow,
        *INTAKE_WORKFLOWS,
        *ASSESSMENT_WORKFLOWS,
        *APPROVAL_PACK_WORKFLOWS,
        *PROJECTION_WORKFLOWS,
    )
    activities = (
        *control_activity_registrations(system, agent_runs),
        *recurring_activity_registrations(recurring),
        *chase_activity_registrations(chase),
        *intake_control_activity_registrations(intake),
        *assessment_control_activity_registrations(assessment),
        *approval_activity_registrations(approval),
        *projection_control_activity_registrations(projection),
    )
    return workflows, activities


def _effects_bindings(app: Any) -> tuple[tuple[type, ...], tuple[Any, ...]]:
    for dependency in ("agent_runtime", "assessment_agent", "projection_agent"):
        if not hasattr(app.state, dependency):
            raise ConfigurationError(f"effects Worker dependency is missing: {dependency}")
    intake = IntakeActivities(app)
    assessment = AssessmentActivities(app)
    projection = ProjectionActivities(app)
    return (), (
        *intake_effect_activity_registrations(intake),
        *assessment_effect_activity_registrations(assessment),
        *projection_effect_activity_registrations(projection),
    )


def _role_bindings(
    config: TemporalConfig, dependencies: WorkerDependencies
) -> tuple[tuple[type, ...], tuple[Any, ...]]:
    role = config.worker_role
    if role == "control":
        return _control_bindings(dependencies.app, config.build_id)
    if role == "effects":
        return _effects_bindings(dependencies.app)
    if role == "ledger":
        return (), ledger_activity_registrations(SystemActivities(dependencies.app))
    if role == "docintel":
        if dependencies.docintel_engine is None:
            raise ConfigurationError("docintel Worker dependency is missing: docintel_engine")
        from doc_intel.activities import (
            DocumentIntelligenceActivities,
            docintel_activity_registrations,
        )
        from doc_intel.workflows import DOCINTEL_WORKFLOWS

        activities = DocumentIntelligenceActivities(
            dependencies.docintel_engine, worker_build_id=config.build_id
        )
        return DOCINTEL_WORKFLOWS, docintel_activity_registrations(activities)
    raise ConfigurationError("PACHA_WORKER_ROLE is required")


async def run() -> None:
    """Validate configuration, bind one role, then poll until termination."""

    config = TemporalConfig.from_environ(require_worker_role=True)
    configure_control_logging(config)
    telemetry = build_runtime_telemetry(config)
    dependencies = _dependencies(config)
    client = await build_temporal_client(config, runtime=telemetry)
    _install_temporal_bridge(dependencies.app, TemporalStarter(client, config))
    workflows, activities = _role_bindings(config, dependencies)
    worker = build_worker(
        client,
        config,
        workflows=workflows,
        activities=activities,
    )
    await worker.run()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()


__all__ = ["WorkerDependencies", "main", "run"]
