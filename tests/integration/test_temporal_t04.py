"""T04 acceptance for the PRD-01 Temporal migration.

This suite is intentionally owner-pinned before T04 implementation.  It
asserts orchestration replacement only; PACKET-04/05 continue to own the
document-intelligence product behaviour.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

from temporalio import activity
from temporalio.common import VersioningBehavior
from temporalio.testing import WorkflowEnvironment

from orchestration.contracts import ControlCommand, ControlResult
from orchestration.history import decoded_history_blob
from orchestration.ids import docintel_workflow_ref
from orchestration.worker import build_worker
from support.temporal import (
    DOCUMENT_REF,
    PRIVACY_SENTINELS,
    RUN_REF,
    local_config,
    static_data_converter,
)

ACTIVITY_NAMES = (
    "docintel_normalize",
    "docintel_classify",
    "docintel_split",
    "docintel_extract",
    "docintel_cite",
    "docintel_validate",
    "docintel_commit",
    "docintel_consistency",
)
_TRACE: list[tuple[str, dict]] = []


def _fake_activity(name: str, index: int):
    async def run(command: ControlCommand) -> ControlResult:
        _TRACE.append((name, command.as_control_mapping()))
        return ControlResult(
            status="completed" if index == len(ACTIVITY_NAMES) else "running",
            attempt_no=index,
        )

    run.__name__ = f"fake_{name}"
    return activity.defn(name=name)(run)


FAKE_ACTIVITIES = tuple(
    _fake_activity(name, index)
    for index, name in enumerate(ACTIVITY_NAMES, start=1)
)


def _workflow_class():
    module = importlib.import_module("doc_intel.workflows")
    return module.DocumentIntelligenceWorkflow


def test_t04_declares_the_exact_pinned_workflow_and_activity_surface() -> None:
    workflow_class = _workflow_class()
    definition = getattr(workflow_class, "__temporal_workflow_definition", None)
    assert definition is not None
    assert definition.name == "DocumentIntelligenceWorkflow"
    assert definition.versioning_behavior is VersioningBehavior.PINNED

    source = inspect.getsource(importlib.import_module("doc_intel.workflows"))
    assert tuple(name for name in ACTIVITY_NAMES if f'"{name}"' in source) == ACTIVITY_NAMES
    assert "pacha-{env}-docintel-v1" not in source
    assert "document_ref" in source


def test_document_received_has_one_stable_start_mapping() -> None:
    starter = importlib.import_module("orchestration.starter")
    mappings = [
        mapping
        for mapping in starter.TEMPORAL_INTENT_MAPPINGS
        if mapping.event_type == "document.received"
    ]
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.action == "start"
    assert mapping.signal_name is None
    assert mapping.workflow_type in {
        "DocumentIntelligenceWorkflow",
        _workflow_class(),
    }
    event = SimpleNamespace(payload={"document_id": DOCUMENT_REF})
    assert mapping.workflow_id_builder(event) == docintel_workflow_ref(DOCUMENT_REF)


def test_real_temporal_runs_the_exact_stage_order_on_the_docintel_queue() -> None:
    workflow_class = _workflow_class()
    _TRACE.clear()

    async def scenario() -> tuple[ControlResult, bytes]:
        config = local_config()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=static_data_converter(config),
        ) as temporal:
            control_worker = build_worker(
                temporal.client,
                config,
                role="control",
                workflows=[workflow_class],
            )
            docintel_worker = build_worker(
                temporal.client,
                config,
                role="docintel",
                activities=FAKE_ACTIVITIES,
            )
            async with control_worker, docintel_worker:
                workflow_id = str(docintel_workflow_ref(DOCUMENT_REF))
                result = await temporal.client.execute_workflow(
                    workflow_class.run,
                    ControlCommand(run_ref=RUN_REF, document_ref=DOCUMENT_REF),
                    id=workflow_id,
                    task_queue=config.task_queue("control"),
                )
                history = await temporal.client.get_workflow_handle(
                    workflow_id
                ).fetch_history()
                decoded = await decoded_history_blob(
                    history,
                    temporal.client.data_converter.payload_codec,
                )
                return result, decoded

    result, decoded = asyncio.run(scenario())
    assert result.status == "completed"
    assert [name for name, _ in _TRACE] == list(ACTIVITY_NAMES)
    assert all(
        payload["document_ref"] == DOCUMENT_REF and set(payload) <= {
            "run_ref",
            "document_ref",
            "attempt_no",
        }
        for _, payload in _TRACE
    )
    for sentinel in PRIVACY_SENTINELS.values():
        assert sentinel.encode() not in decoded


def test_t04_removes_only_the_docintel_celery_scheduler() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = (root / "platform/doc_intel/runtime.py").read_text()
    assert "CeleryStageScheduler" not in runtime
    assert "doc_intel.tasks" not in runtime
    assert not (root / "platform/doc_intel/tasks.py").exists()

    requirements = (root / "requirements.txt").read_text().lower()
    assert "celery" in requirements, "global Celery removal belongs to T08"
