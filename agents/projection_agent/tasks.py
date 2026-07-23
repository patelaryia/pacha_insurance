"""Package-local Celery Beat registration for weekly paste readback sampling.

The schedule is pack data (register #271). The task body is the same
synchronous engine acceptance and operations drive directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from celery.schedules import crontab

from claim_core import celery_app

BEAT_ENTRY = "projection-agent-weekly-paste-readback"
TASK_NAME = "projection_agent.sample_paste_readbacks"
#: PACKET-21 §8/§14. The reaper interval is pack data; the nightly drift slot is
#: registered but stays unscheduled while its EAT time is uncaptured (#290).
REAPER_BEAT_ENTRY = "projection-agent-lease-reaper"
REAPER_TASK_NAME = "projection_agent.reap_leases"
DRIFT_BEAT_ENTRY = "projection-agent-nightly-drift"
DRIFT_TASK_NAME = "projection_agent.nightly_drift"

_SERVICE: Any | None = None


def _scheduler_slot(sampling: Any) -> tuple[str, int, int]:
    """Convert one configured weekly wall time into Celery Beat's timezone."""

    source_timezone = ZoneInfo(sampling.timezone)
    scheduler_timezone = celery_app.timezone
    source_weekday = (
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
    ).index(sampling.day_of_week)
    year = datetime.now(UTC).year
    converted: set[tuple[str, int, int]] = set()
    day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for month in (1, 4, 7, 10):
        first = datetime(year, month, 1)
        day = 1 + (source_weekday - first.weekday()) % 7
        local = datetime(
            year,
            month,
            day,
            sampling.hour,
            sampling.minute,
            tzinfo=source_timezone,
        )
        scheduler_time = local.astimezone(scheduler_timezone)
        converted.add(
            (
                day_names[scheduler_time.weekday()],
                scheduler_time.hour,
                scheduler_time.minute,
            )
        )
    if len(converted) != 1:
        raise ValueError(
            "sampling timezone cannot be represented by one scheduler crontab"
        )
    return converted.pop()


@celery_app.task(name=TASK_NAME)
def sample_paste_readbacks() -> dict[str, Any]:
    """Run the deterministic 10% sampler over completed paste-assist rows."""

    if _SERVICE is None:
        raise RuntimeError("projection agent task runtime is not configured")
    return _SERVICE.sample_paste_readbacks()


@celery_app.task(name=REAPER_TASK_NAME)
def reap_leases() -> dict[str, Any]:
    """Recover stale executing rows by exact readback. It never re-writes."""

    if _SERVICE is None:
        raise RuntimeError("projection agent task runtime is not configured")
    return _SERVICE.rpa.reap_leases()


@celery_app.task(name=DRIFT_TASK_NAME)
def nightly_drift() -> dict[str, Any]:
    """One idempotent standing-drift cycle. Pending registry does nothing."""

    if _SERVICE is None:
        raise RuntimeError("projection agent task runtime is not configured")
    return _SERVICE.drift.run()


def configure_weekly_task(service: Any) -> None:
    """Bind the current service and install every pack-configured slot."""

    global _SERVICE  # noqa: PLW0603 - Celery task bindings are process-local
    _SERVICE = service
    sampling = service.operations.sampling
    scheduler_day, scheduler_hour, scheduler_minute = _scheduler_slot(sampling)
    schedule = dict(celery_app.conf.beat_schedule or {})
    schedule[BEAT_ENTRY] = {
        "task": TASK_NAME,
        "schedule": crontab(
            day_of_week=scheduler_day,
            hour=scheduler_hour,
            minute=scheduler_minute,
            app=celery_app,
        ),
    }
    schedule[REAPER_BEAT_ENTRY] = {
        "task": REAPER_TASK_NAME,
        "schedule": float(service.operations.runtime.runner.reaper_seconds),
    }
    drift = service.operations.drift
    if drift.schedulable and service.operations.runtime.drift_schedule_status == "live":
        schedule[DRIFT_BEAT_ENTRY] = {
            "task": DRIFT_TASK_NAME,
            "schedule": crontab(
                day_of_week=drift.day_of_week,
                hour=drift.hour,
                minute=drift.minute,
                app=celery_app,
            ),
        }
    else:
        # Visibly disabled: the nightly EAT time is not in the source documents.
        schedule.pop(DRIFT_BEAT_ENTRY, None)
    celery_app.conf.beat_schedule = schedule


__all__ = [
    "BEAT_ENTRY",
    "DRIFT_BEAT_ENTRY",
    "DRIFT_TASK_NAME",
    "REAPER_BEAT_ENTRY",
    "REAPER_TASK_NAME",
    "TASK_NAME",
    "configure_weekly_task",
    "nightly_drift",
    "reap_leases",
    "sample_paste_readbacks",
]
