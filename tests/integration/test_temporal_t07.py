"""Owner-pinned Temporal T07 contract for finite recurring schedules."""

from __future__ import annotations

import importlib
import inspect

from temporalio.common import VersioningBehavior

EXPECTED_SCHEDULES = {
    "pacha-test-outbox-drain-v1": ("30s", "SKIP", "5m"),
    "pacha-test-ledger-drain-v1": ("10s", "SKIP", "5m"),
    "pacha-test-sla-evaluate-v1": ("5m", "SKIP", "30m"),
    "pacha-test-ledger-verify-v1": ("01:00 UTC daily", "BUFFER_ONE", "24h"),
    "pacha-test-notify-digest-v1": ("05:00 UTC daily", "BUFFER_ONE", "24h"),
    "pacha-test-graph-delta-v1": ("60s", "SKIP", "5m"),
    "pacha-test-graph-renew-v1": ("71h", "BUFFER_ONE", "24h"),
    "pacha-test-eval-weekly-v1": ("pack weekly", "BUFFER_ONE", "7d"),
    "pacha-test-paste-readback-v1": (
        "Monday 05:00 UTC",
        "BUFFER_ONE",
        "24h",
    ),
}

WRAPPER_NAMES = (
    "NotifyDigestWorkflow",
    "GraphDeltaWorkflow",
    "GraphRenewalWorkflow",
    "WeeklyEvaluationWorkflow",
    "PasteReadbackSampleWorkflow",
)


def _definition(cls):
    definition = getattr(cls, "__temporal_workflow_definition", None)
    assert definition is not None
    assert definition.versioning_behavior is VersioningBehavior.PINNED
    return definition


def test_t07_exports_bootstrap_and_all_five_finite_pinned_wrappers():
    package = importlib.import_module("orchestration")
    schedules = importlib.import_module("orchestration.schedules")
    assert package.bootstrap_schedules is schedules.bootstrap_schedules
    for name in WRAPPER_NAMES:
        assert _definition(getattr(schedules, name)).name == name

    source = inspect.getsource(schedules)
    for name in WRAPPER_NAMES:
        assert f"class {name}" in source
    assert source.count("execute_activity") == 5
    assert "continue_as_new" not in source
    assert "cron_schedule" not in source


def test_t07_declares_all_nine_exact_schedule_definitions():
    schedules = importlib.import_module("orchestration.schedules")
    definitions = schedules.schedule_definitions(
        env="test",
        weekly_time="pack weekly",
    )
    observed = {
        item.schedule_id: (
            item.timing,
            item.overlap_policy.name,
            item.catchup_window,
        )
        for item in definitions
    }
    assert observed == EXPECTED_SCHEDULES
    assert all(item.pause_on_failure is False for item in definitions)


def test_t07_bootstrap_refuses_drift_and_missing_graph_registration():
    schedules = importlib.import_module("orchestration.schedules")
    source = inspect.getsource(schedules)
    assert "GRAPH_SERVICE_NOT_INSTALLED" in source
    assert "ScheduleOverlapPolicy.SKIP" in source
    assert "ScheduleOverlapPolicy.BUFFER_ONE" in source
    assert "describe" in source
    assert "create" in source
    assert ".update(" not in source
    assert ".delete(" not in source
    assert "pause_on_failure=False" in source

