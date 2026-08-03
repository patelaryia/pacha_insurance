"""Fail-closed JSON logging for production Workflow and Activity processes."""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from orchestration.config import TemporalConfig
from orchestration.errors import ConfigurationError

CONTROL_LOG_FIELDS: Final[tuple[str, ...]] = (
    "workflow_id",
    "workflow_run_id",
    "workflow_type",
    "activity_type",
    "run_ref",
    "step_id",
    "attempt",
    "task_queue",
    "build_id",
    "status",
    "error_code",
    "duration_ms",
)


class ControlJsonFormatter(logging.Formatter):
    """Serialize only the binding control-field allowlist.

    The human message, interpolation arguments, exception text and traceback
    are deliberately not serialized. Existing Activities may log a provider or
    domain exception; this process boundary ensures those values never reach
    CloudWatch even if a lower layer forgot to redact its message.
    """

    def __init__(self, config: TemporalConfig) -> None:
        super().__init__()
        self._defaults = {
            "task_queue": config.task_queue(config.worker_role or "control"),
            "build_id": config.build_id,
        }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = dict(self._defaults)
        for field in CONTROL_LOG_FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, bool) or not isinstance(value, str | int):
                continue
            payload[field] = value
        if "status" not in payload:
            payload["status"] = record.levelname.lower()
        if record.exc_info is not None and "error_code" not in payload:
            payload["error_code"] = "unclassified_exception"
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_control_logging(config: TemporalConfig) -> None:
    """Replace root handlers before any application dependency is constructed."""

    if not config.is_production_like or not config.is_cloud:
        raise ConfigurationError("production logging requires staging/prod cloud mode")
    handler = logging.StreamHandler()
    handler.setFormatter(ControlJsonFormatter(config))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


__all__ = ["CONTROL_LOG_FIELDS", "ControlJsonFormatter", "configure_control_logging"]
