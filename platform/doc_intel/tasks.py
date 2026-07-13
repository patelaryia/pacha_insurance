"""One named Celery task per stage, bootstrapped independently in each worker."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from claim_core import celery_app
from doc_intel.stages import STAGES


@lru_cache(maxsize=1)
def get_worker_runtime() -> Any:
    """Construct a fresh-process runtime without depending on FastAPI module globals."""

    from doc_intel.runtime import build_worker_runtime

    return build_worker_runtime()


def _run(document_id: str, stage: str) -> dict[str, Any]:
    runtime = get_worker_runtime()
    return runtime.engine.process_stage(document_id, stage, schedule_next=True)


PIPELINE_TASKS: dict[str, Any] = {}


def _stage_callable(stage: str):
    def run(document_id: str) -> dict[str, Any]:
        return _run(document_id, stage)

    run.__name__ = f"run_{stage.casefold()}"
    return run


for _stage in STAGES:
    task = celery_app.task(name=f"doc_intel.{_stage.casefold()}")(_stage_callable(_stage))
    PIPELINE_TASKS[_stage] = task
