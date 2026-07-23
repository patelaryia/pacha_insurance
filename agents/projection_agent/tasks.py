"""Package-local Celery Beat registration for weekly paste readback sampling.

The schedule is pack data (register #271). The task body is the same
synchronous engine acceptance and operations drive directly.
"""

from __future__ import annotations

from typing import Any

from celery.schedules import crontab

from claim_core import celery_app

BEAT_ENTRY = "projection-agent-weekly-paste-readback"
TASK_NAME = "projection_agent.sample_paste_readbacks"

_SERVICE: Any | None = None


@celery_app.task(name=TASK_NAME)
def sample_paste_readbacks() -> dict[str, Any]:
    """Run the deterministic 10% sampler over completed paste-assist rows."""

    if _SERVICE is None:
        raise RuntimeError("projection agent task runtime is not configured")
    return _SERVICE.sample_paste_readbacks()


def configure_weekly_task(service: Any) -> None:
    """Bind the current service and install the pack-configured weekly slot."""

    global _SERVICE  # noqa: PLW0603 - Celery task bindings are process-local
    _SERVICE = service
    sampling = service.operations.sampling
    schedule = dict(celery_app.conf.beat_schedule or {})
    schedule[BEAT_ENTRY] = {
        "task": TASK_NAME,
        "schedule": crontab(
            day_of_week=sampling.day_of_week,
            hour=sampling.hour,
            minute=sampling.minute,
        ),
        "options": {"timezone": sampling.timezone},
    }
    celery_app.conf.beat_schedule = schedule


__all__ = ["BEAT_ENTRY", "TASK_NAME", "configure_weekly_task", "sample_paste_readbacks"]
