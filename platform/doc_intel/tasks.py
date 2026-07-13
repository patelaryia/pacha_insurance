"""One named Celery task per idempotent document-intelligence stage."""

from __future__ import annotations

from typing import Any

from claim_core.celery_app import celery_app
from doc_intel.stages import STAGES

_engine: Any = None


def configure_runtime(engine: Any) -> None:
    global _engine
    _engine = engine


def _run(document_id: str, stage: str) -> dict[str, Any]:
    if _engine is None:
        raise RuntimeError("doc-intel task runtime is not configured")
    return _engine.process_stage(document_id, stage)


PIPELINE_TASKS = {}


def _stage_callable(stage: str):
    def run(document_id: str) -> dict[str, Any]:
        return _run(document_id, stage)

    run.__name__ = f"run_{stage.casefold()}"
    return run


for _stage in STAGES:
    task = celery_app.task(name=f"doc_intel.{_stage.casefold()}")(_stage_callable(_stage))
    PIPELINE_TASKS[_stage] = task
