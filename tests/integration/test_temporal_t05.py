"""Owner-pinned Temporal T05 contract for intake and assessment."""

from __future__ import annotations

import importlib
import inspect

from temporalio.common import VersioningBehavior

INTAKE_ACTIVITIES = (
    "intake_create_claim",
    "intake_ingest",
    "intake_populate",
    "intake_dupe_check",
    "intake_late_check",
    "intake_acknowledge",
    "intake_checklist",
    "intake_triage",
)
ASSESSMENT_ACTIVITIES = (
    "assessment_prepare",
    "assessment_mode_shadow",
    "assessment_apply_mode_review",
    "assessment_dispatch",
    "assessment_parse_report",
    "assessment_cascade",
    "assessment_record_terminal",
)


def _definition(cls):
    definition = getattr(cls, "__temporal_workflow_definition", None)
    assert definition is not None
    assert definition.versioning_behavior is VersioningBehavior.PINNED
    return definition


def test_t05_declares_the_exact_pinned_workflow_and_activity_surfaces():
    intake_workflows = importlib.import_module("intake_agent.workflows")
    assessment_workflows = importlib.import_module("assessment_agent.workflows")

    assert _definition(intake_workflows.IntakeWorkflow).name == "IntakeWorkflow"
    assert (
        _definition(assessment_workflows.AssessmentWorkflow).name
        == "AssessmentWorkflow"
    )
    intake_source = inspect.getsource(intake_workflows)
    assessment_source = inspect.getsource(assessment_workflows)
    assert tuple(
        name for name in INTAKE_ACTIVITIES if f'"{name}"' in intake_source
    ) == INTAKE_ACTIVITIES
    assert tuple(
        name for name in ASSESSMENT_ACTIVITIES if f'"{name}"' in assessment_source
    ) == ASSESSMENT_ACTIVITIES


def test_t05_ids_and_outbox_routes_are_stable_and_domain_specific():
    ids = importlib.import_module("orchestration.ids")
    starter = importlib.import_module("orchestration.starter")
    assert str(ids.intake_workflow_ref("01H00000000000000000000001")) == (
        "pacha.intake.01H00000000000000000000001"
    )
    assert str(ids.assessment_workflow_ref("01H00000000000000000000002")) == (
        "pacha.assessment.01H00000000000000000000002"
    )
    routes = {
        (mapping.event_type, mapping.action, mapping.signal_name)
        for mapping in starter.TEMPORAL_INTENT_MAPPINGS
    }
    assert {
        ("intake.requested", "start", None),
        ("intake.document_ready", "signal", "document_received"),
        ("intake.review_resolved", "signal", "review_resolved"),
        ("intake.claim_terminal", "signal", "claim_terminal"),
        ("assessment.workflow_requested", "start", None),
        ("assessment.review_resolved", "signal", "review_resolved"),
        ("assessment.report_ready", "signal", "document_received"),
        ("assessment.claim_terminal", "signal", "claim_terminal"),
    } <= routes


def test_t05_removes_the_legacy_business_orchestration_entry_points():
    intake_source = inspect.getsource(importlib.import_module("intake_agent.flow"))
    trigger_source = inspect.getsource(importlib.import_module("assessment_agent.trigger"))
    for forbidden in (
        "agent_runtime.start_run(",
        "agent_runtime.run(",
        "agent_runtime.runner.set_claim_id(",
    ):
        assert forbidden not in intake_source
        assert forbidden not in trigger_source


def test_t05_workflows_carry_only_control_contracts_and_dispatch_once():
    intake_source = inspect.getsource(importlib.import_module("intake_agent.workflows"))
    assessment_source = inspect.getsource(
        importlib.import_module("assessment_agent.workflows")
    )
    joined = intake_source + assessment_source
    assert "ControlCommand" in joined
    assert "ControlSignal" in joined
    assert "maximum_attempts=1" in assessment_source
    for forbidden in (
        "estimate_total",
        "registration",
        "recipient",
        "body_text",
        "from_addr",
        "to_addrs",
    ):
        assert forbidden not in joined
