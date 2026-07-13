"""Thin Celery/Beat wiring; all task logic remains synchronous in engine modules."""

from __future__ import annotations

import os
from importlib import import_module
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


def _ensure_runtime() -> None:
    if _runtime:
        return
    reference = os.environ.get("PACHA_WORKER_RUNTIME_FACTORY")
    if not reference or ":" not in reference:
        raise RuntimeError("PACHA_WORKER_RUNTIME_FACTORY=module:factory is required")
    module_name, attribute = reference.split(":", 1)
    getattr(import_module(module_name), attribute)()
    if not _runtime:
        raise RuntimeError("worker runtime factory did not configure claim-core services")


def configure_runtime(*, dispatcher: Any, sla_engine: Any, ledger: Any) -> None:
    """Bind application-owned synchronous engines for worker execution."""

    _runtime.update(dispatcher=dispatcher, sla_engine=sla_engine, ledger=ledger)


@celery_app.task(name="claim_core.dispatch_events")
def dispatch_events() -> int:
    _ensure_runtime()
    return _runtime["dispatcher"].dispatch_once(
        _runtime["dispatcher"].consumer_names - {"ledger"}
    )


@celery_app.task(name="claim_core.dispatch_ledger")
def dispatch_ledger() -> int:
    _ensure_runtime()
    return _runtime["dispatcher"].dispatch_once({"ledger"})


@celery_app.task(name="claim_core.evaluate_slas")
def evaluate_slas() -> int:
    _ensure_runtime()
    return _runtime["sla_engine"].evaluate()


@celery_app.task(name="claim_core.verify_ledger")
def verify_ledger() -> dict[str, bool | int | None]:
    _ensure_runtime()
    return _runtime["ledger"].run_nightly_verification()
