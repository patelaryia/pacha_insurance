"""Owner-pinned Temporal T06 contract for approval packs and projection."""

from __future__ import annotations

import importlib
import inspect

from temporalio.common import VersioningBehavior

APPROVAL_ACTIVITIES = (
    "approval_resolve_manifest",
    "approval_merge",
    "approval_generate_note",
    "approval_grade_and_queue",
    "approval_apply_review",
    "approval_prepare_signature",
    "approval_finalize_signature",
    "approval_record_terminal",
)
PROJECTION_ACTIVITIES = (
    "projection_prepare",
    "projection_execute_or_stage",
    "projection_readback",
    "projection_reconcile",
    "projection_record_terminal",
)


def _definition(cls):
    definition = getattr(cls, "__temporal_workflow_definition", None)
    assert definition is not None
    assert definition.versioning_behavior is VersioningBehavior.PINNED
    return definition


def test_t06_declares_the_exact_pinned_workflow_and_activity_surfaces():
    approval = importlib.import_module("approval_pack_agent.workflows")
    projection = importlib.import_module("projection_agent.workflows")
    assert _definition(approval.ApprovalPackWorkflow).name == "ApprovalPackWorkflow"
    assert _definition(projection.ProjectionWorkflow).name == "ProjectionWorkflow"
    approval_source = inspect.getsource(approval)
    projection_source = inspect.getsource(projection)
    assert tuple(
        name for name in APPROVAL_ACTIVITIES if f'"{name}"' in approval_source
    ) == APPROVAL_ACTIVITIES
    assert tuple(
        name for name in PROJECTION_ACTIVITIES if f'"{name}"' in projection_source
    ) == PROJECTION_ACTIVITIES


def test_t06_ids_and_start_routes_are_stable():
    ids = importlib.import_module("orchestration.ids")
    starter = importlib.import_module("orchestration.starter")
    assert str(ids.approval_pack_workflow_ref("01H00000000000000000000003")) == (
        "pacha.approval-pack.01H00000000000000000000003"
    )
    assert str(ids.projection_workflow_ref("01H00000000000000000000004")) == (
        "pacha.projection.01H00000000000000000000004"
    )
    routes = {
        (mapping.event_type, mapping.action, mapping.signal_name)
        for mapping in starter.TEMPORAL_INTENT_MAPPINGS
    }
    assert {
        ("approval.workflow_requested", "start", None),
        ("approval.review_resolved", "signal", "review_resolved"),
        ("projection.workflow_requested", "start", None),
        ("projection.review_resolved", "signal", "review_resolved"),
    } <= routes


def test_t06_effects_are_single_attempt_and_readback_is_separate():
    approval = inspect.getsource(
        importlib.import_module("approval_pack_agent.workflows")
    )
    projection = inspect.getsource(importlib.import_module("projection_agent.workflows"))
    assert "projection_execute_or_stage" in projection
    assert "projection_readback" in projection
    assert projection.index("projection_execute_or_stage") < projection.index(
        "projection_readback"
    )
    assert "maximum_attempts=1" in projection
    assert "maximum_attempts=1" in approval


def test_t06_history_contract_has_no_artifact_or_claim_value_slots():
    source = inspect.getsource(
        importlib.import_module("approval_pack_agent.workflows")
    ) + inspect.getsource(importlib.import_module("projection_agent.workflows"))
    for forbidden in (
        "pdf_bytes",
        "html_bytes",
        "amount_payable",
        "reserve_total",
        "readback_value",
        "recipient",
    ):
        assert forbidden not in source
    assert "ControlCommand" in source
    assert "ControlSignal" in source
