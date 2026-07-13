"""Package-local Celery Beat registration for weekly corpus evaluation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from claim_core import celery_app

_HARNESS: Any | None = None


@celery_app.task(name="eval_harness.run_weekly_corpus")
def run_weekly_corpus() -> dict[str, Any]:
    """Run the same synchronous engine used by operations and acceptance."""

    if _HARNESS is None:
        raise RuntimeError("eval harness task runtime is not configured")
    return asdict(_HARNESS.corpus.run_weekly(actor="agent:eval"))


def configure_weekly_task(harness: Any) -> None:
    """Bind the current app harness and install one pack-configured interval."""

    global _HARNESS  # noqa: PLW0603 - Celery task bindings are process-local
    _HARNESS = harness
    config = harness.corpus.weekly_config
    schedule = dict(celery_app.conf.beat_schedule or {})
    schedule.pop("eval-harness-weekly-corpus", None)
    if config["enabled"]:
        schedule["eval-harness-weekly-corpus"] = {
            "task": "eval_harness.run_weekly_corpus",
            "schedule": config["interval_seconds"],
        }
    celery_app.conf.beat_schedule = schedule


__all__ = ["configure_weekly_task", "run_weekly_corpus"]
