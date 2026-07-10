"""Thin Celery/Beat wiring; all task logic remains synchronous in engine modules."""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "claim_core",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
)
celery_app.conf.update(
    beat_schedule={
        "dispatch-events": {
            "task": "claim_core.dispatch_events",
            "schedule": 2.0,
        },
        "dispatch-ledger": {
            "task": "claim_core.dispatch_ledger",
            "schedule": 2.0,
        },
        "evaluate-slas": {
            "task": "claim_core.evaluate_slas",
            "schedule": 300.0,
        },
        "verify-ledger-nightly": {
            "task": "claim_core.verify_ledger",
            "schedule": crontab(hour=1, minute=0),
        },
    },
    task_routes={
        "claim_core.dispatch_ledger": {"queue": "ledger"},
    },
)

_runtime: dict[str, Any] = {}


def configure_runtime(*, dispatcher: Any, sla_engine: Any, ledger: Any) -> None:
    """Bind application-owned synchronous engines for worker execution."""

    _runtime.update(dispatcher=dispatcher, sla_engine=sla_engine, ledger=ledger)


@celery_app.task(name="claim_core.dispatch_events")
def dispatch_events() -> int:
    return _runtime["dispatcher"].dispatch_once(
        _runtime["dispatcher"].consumer_names - {"ledger"}
    )


@celery_app.task(name="claim_core.dispatch_ledger")
def dispatch_ledger() -> int:
    return _runtime["dispatcher"].dispatch_once({"ledger"})


@celery_app.task(name="claim_core.evaluate_slas")
def evaluate_slas() -> int:
    return _runtime["sla_engine"].evaluate()


@celery_app.task(name="claim_core.verify_ledger")
def verify_ledger() -> dict[str, bool | int | None]:
    return _runtime["ledger"].run_nightly_verification()
