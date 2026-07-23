"""Public PRD-09 projection agent boundary (register #261).

Install after ``build_eval_harness``, ``build_review_queue``,
``build_agent_runtime``, and the console identity/RBAC install. The builder is
idempotent and exposes the curated facade as ``app.state.projection_agent``.

PACKET-20 owns the durable projection substrate and permanent paste-assist
mode only. It registers no adapter, no executor, and no external write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from claim_core import Base
from projection_agent.api import build_router
from projection_agent.config import (
    OPERATION_IDS,
    Operation,
    OperationConfigError,
    OperationRegistry,
)
from projection_agent.models import Projection
from projection_agent.paste import PasteEngine, encode_copy_value
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


def build_projection_agent(
    app: Any,
    *,
    operation_root: Path | None = None,
) -> ProjectionService:
    """Build PRD-09 slice 1 after its four Phase-1/2 dependencies."""

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
    _retire_legacy_capabilities(app.state.engine)
    Base.metadata.create_all(app.state.engine, tables=list(projection_tables()))
    service = ProjectionService(app, registry)
    _register_readback_validators(app, registry)
    app.state.dispatcher.register_consumer("projection_agent", service.consume)
    app.include_router(build_router(service))
    configure_weekly_task(service)
    app.state.projection_agent = service
    service.backfill(actor="system")
    return service


__all__ = [
    "LEGACY_CAPABILITY_IDS",
    "LegacyProjectionCapabilityInUse",
    "OPERATION_IDS",
    "Operation",
    "OperationConfigError",
    "OperationRegistry",
    "PasteEngine",
    "ProjectionAgent",
    "ProjectionResult",
    "ProjectionService",
    "ProjectionView",
    "build_projection_agent",
    "encode_copy_value",
    "projection_tables",
]
