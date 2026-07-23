"""Public PRD-09 projection agent boundary (register #261).

Install after ``build_eval_harness``, ``build_review_queue``,
``build_agent_runtime``, and the console identity/RBAC install. The builder is
idempotent and exposes the curated facade as ``app.state.projection_agent``.

PACKET-20 owns the durable projection substrate and permanent paste-assist
mode. PACKET-21 adds the RPA runtime and the zero-silent-divergence control
plane: the adapter contracts, the single deferred AR-2 executor, the
authenticated runner control API, reconciliation, and the divergence lifecycle.

Nothing in the production motor pack is activated by any of it: every ICON and
EDMS row stays `pending_capture`/`blocked_on_inputs`, no target endpoint or
credential reference is installed, and the internal runner routes are not
mounted without an infra-supplied machine authenticator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from claim_core import Base
from projection_agent.adapters import (
    Adapter,
    AdapterHealth,
    AdapterUnavailable,
    OpResult,
)
from projection_agent.api import build_review_router, build_router
from projection_agent.config import (
    OPERATION_IDS,
    Operation,
    OperationConfigError,
    OperationRegistry,
)
from projection_agent.models import Projection
from projection_agent.paste import PasteEngine, encode_copy_value
from projection_agent.reconcile import ReconciliationEngine
from projection_agent.rpa import RPA_ACTION_TYPE, RpaCoordinator, RunnerAuthenticator
from projection_agent.runner_api import InProcessControlPlane, build_runner_router
from projection_agent.service import ProjectionResult, ProjectionService, ProjectionView
from projection_agent.tasks import configure_weekly_task

ProjectionAgent = ProjectionService

#: The six provisional bare capability ids PACKET-08 seeded under register #71.
#: Register #262 replaces them with canonical `project.<operation>` ids.
LEGACY_CAPABILITY_IDS = (
    "icon.claim_register",
    "icon.reserve_adjust",
    "icon.assessor_payment_request",
    "icon.payment_voucher",
    "edms.attach_and_tag",
    "edms.claims_workflow",
)


class LegacyProjectionCapabilityInUse(RuntimeError):
    """A provisional bare capability id carries durable production evidence."""


def projection_tables() -> tuple[Any, ...]:
    """Expose the PRD-09 §9.2 table to Alembic and dependent packages."""

    return (Projection.__table__,)


def _legacy_evidence(engine: Any) -> dict[str, list[str]]:
    """Prove the provisional ids have no durable evidence before removing them.

    Append-only history is never rewritten: if any run, grade, promotion, or
    event names a bare id, startup fails closed for owner migration.
    """

    probes = (
        ("agent_runs", "SELECT 1 FROM agent_runs WHERE capability_id = :id LIMIT 1"),
        (
            "autonomy_changes",
            "SELECT 1 FROM autonomy_changes WHERE capability_id = :id LIMIT 1",
        ),
        (
            "grader_runs",
            "SELECT 1 FROM grader_runs WHERE claim_id IS NOT NULL AND "
            "CAST(subject_ref AS TEXT) LIKE :like LIMIT 1",
        ),
        (
            "events",
            "SELECT 1 FROM events WHERE CAST(payload AS TEXT) LIKE :like LIMIT 1",
        ),
    )
    found: dict[str, list[str]] = {}
    inspector_tables = set()
    with engine.connect() as connection:
        for table, _statement in probes:
            try:
                connection.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            except Exception:  # noqa: BLE001 - an absent table is simply no evidence
                connection.rollback()
                continue
            inspector_tables.add(table)
        for capability_id in LEGACY_CAPABILITY_IDS:
            hits: list[str] = []
            for table, statement in probes:
                if table not in inspector_tables:
                    continue
                row = connection.execute(
                    text(statement),
                    {"id": capability_id, "like": f'%"capability_id": "{capability_id}"%'},
                ).first()
                if row is not None:
                    hits.append(table)
            if hits:
                found[capability_id] = hits
    return found


def _retire_legacy_capabilities(engine: Any) -> list[str]:
    evidence = _legacy_evidence(engine)
    if evidence:
        raise LegacyProjectionCapabilityInUse(
            "LEGACY_PROJECTION_CAPABILITY_IN_USE: "
            f"{sorted(evidence)} carry durable evidence and need owner migration"
        )
    retired: list[str] = []
    with engine.begin() as connection:
        try:
            connection.execute(text("SELECT 1 FROM capabilities LIMIT 1"))
        except Exception:  # noqa: BLE001 - no capability table yet, nothing to retire
            return retired
        for capability_id in LEGACY_CAPABILITY_IDS:
            result = connection.execute(
                text("DELETE FROM capabilities WHERE id = :id"),
                {"id": capability_id},
            )
            if result.rowcount:
                retired.append(capability_id)
    return retired


def _assert_grader_coverage(pack_root: Path, registry: OperationRegistry) -> None:
    """ED-7: projection readback must map to a registered critical grader."""

    path = pack_root / "eval" / "grader_map.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise OperationConfigError(f"invalid grader map: {error}") from error
    outputs = (payload or {}).get("output_types")
    if not isinstance(outputs, dict):
        raise OperationConfigError("grader map requires an output_types mapping")
    row = outputs.get("projection_readback")
    if not isinstance(row, dict):
        raise OperationConfigError("grader map does not register projection_readback")
    graders = row.get("graders")
    if not isinstance(graders, list) or not any(
        isinstance(entry, dict)
        and entry.get("severity") == "critical"
        and isinstance(entry.get("id"), str)
        for entry in graders
    ):
        raise OperationConfigError(
            "projection_readback requires at least one critical grader"
        )
    paths = row.get("field_paths")
    declared = {
        readback.into
        for operation in registry.all()
        if operation.click_path is not None
        for readback in operation.click_path.readback
    }
    if not isinstance(paths, list) or not declared <= set(paths):
        raise OperationConfigError(
            "grader map does not cover every declared readback field path"
        )


def _register_readback_validators(app: Any, registry: OperationRegistry) -> None:
    """Teach the existing critical G-VAL grader the captured readback formats."""

    graders = getattr(app.state, "eval_harness", None)
    if graders is None:
        return
    for operation in registry.all():
        click_path = operation.click_path
        if click_path is None:
            continue
        for readback in click_path.readback:
            validator = click_path.validators[readback.assert_format]
            if validator.status != "live" or validator.pattern is None:
                continue
            pattern = validator.pattern
            graders.graders.register_external_validator(
                readback.into,
                name=validator.name,
                check=lambda value, pattern=pattern: isinstance(value, str)
                and pattern.fullmatch(value) is not None,
            )


def _assert_no_funds_transfer(registry: OperationRegistry) -> None:
    """PRD-12/GP-1: no payment operation may become an executable slot."""

    for operation_id in ("icon.payment_voucher", "edms.claim_payment", "edms.payment_workflow"):
        operation = registry.get(operation_id)
        if operation.status == "live" or operation.mode == "rpa":
            raise OperationConfigError(
                f"{operation_id} is a PRD-12/GP-1 operation and cannot be registered "
                "as an executable slot"
            )


def build_projection_agent(
    app: Any,
    *,
    operation_root: Path | None = None,
    runner_authenticator: RunnerAuthenticator | None = None,
    adapters: dict[str, Adapter] | None = None,
) -> ProjectionService:
    """Build the PRD-09 projection agent after its four Phase-1/2 dependencies."""

    existing = getattr(app.state, "projection_agent", None)
    if existing is not None:
        return existing
    for dependency in ("claim_service", "dispatcher", "eval_harness", "review_queue"):
        if not hasattr(app.state, dependency):
            raise RuntimeError(
                "build_projection_agent requires the claim service, dispatcher, "
                "eval harness, and review queue"
            )
    if not hasattr(app.state, "agent_runtime"):
        raise RuntimeError("build_projection_agent requires the shared agent runtime")
    if not hasattr(app.state, "console_roles"):
        raise RuntimeError("build_projection_agent requires the installed console RBAC")
    repo = Path(__file__).resolve().parents[2]
    root = (
        Path(operation_root)
        if operation_root is not None
        else repo / "packs" / "motor" / "projection"
    )
    registry = OperationRegistry(root)
    _assert_grader_coverage(root.parent, registry)
    _assert_no_funds_transfer(registry)
    _retire_legacy_capabilities(app.state.engine)
    Base.metadata.create_all(app.state.engine, tables=list(projection_tables()))
    service = ProjectionService(app, registry, runner_authenticator=runner_authenticator)
    service.rpa.adapters.update(adapters or {})
    _register_readback_validators(app, registry)
    # Every live RPA row must meet all six §4 activation conditions at startup;
    # an under-levelled or unbacked definition fails closed rather than pasting.
    for operation in registry.all():
        if operation.is_live_rpa:
            service.rpa.assert_activation(operation)
    app.state.agent_runtime.register_deferred_executor(
        RPA_ACTION_TYPE, service.rpa._deferred_executor
    )
    app.state.eval_harness.graders.activate_gproc(
        lambda capability_id: app.state.agent_runtime.runner.definitions.get(capability_id, ())
    )
    app.state.review_queue.service.register_resolution_validator(
        "PASTE_READBACK_CHECK", service.validate_paste_readback_resolution
    )
    app.state.dispatcher.register_consumer("projection_agent", service.consume)
    app.include_router(build_router(service))
    app.include_router(build_review_router(service))
    if runner_authenticator is not None:
        app.include_router(build_runner_router(service))
    configure_weekly_task(service)
    app.state.projection_agent = service
    service.backfill(actor="system")
    return service


def runner_control_status(service: ProjectionService) -> dict[str, Any]:
    """What the application reports when the runner identity is not installed."""

    runtime = service.operations.runtime
    if service.runner_authenticator is not None:
        return {"status": "mounted", "blocked_on": None}
    return {
        "status": runtime.control_api_auth_status,
        "blocked_on": runtime.control_api_blocked_on,
    }


__all__ = [
    "Adapter",
    "AdapterHealth",
    "AdapterUnavailable",
    "InProcessControlPlane",
    "LEGACY_CAPABILITY_IDS",
    "LegacyProjectionCapabilityInUse",
    "OPERATION_IDS",
    "OpResult",
    "Operation",
    "OperationConfigError",
    "OperationRegistry",
    "PasteEngine",
    "ProjectionAgent",
    "ProjectionResult",
    "ProjectionService",
    "ProjectionView",
    "RPA_ACTION_TYPE",
    "ReconciliationEngine",
    "RpaCoordinator",
    "RunnerAuthenticator",
    "build_projection_agent",
    "encode_copy_value",
    "projection_tables",
    "runner_control_status",
]
