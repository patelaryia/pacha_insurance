"""Production Temporal SDK telemetry with a local, private OTLP collector."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from temporalio.runtime import (
    LoggingConfig,
    OpenTelemetryConfig,
    Runtime,
    TelemetryConfig,
    TelemetryFilter,
)

from orchestration.config import TemporalConfig
from orchestration.errors import ConfigurationError

_LOCAL_OTLP_ENDPOINT: Final[str] = "http://127.0.0.1:4317"


def build_runtime_telemetry(config: TemporalConfig) -> Runtime:
    """Build the one SDK runtime used by a production Worker process.

    The collector is a sidecar in the same Fargate task. The endpoint is fixed
    to loopback so telemetry cannot be redirected to an unreviewed recipient by
    configuration. Workflow payloads are never exported; only SDK metrics and
    the allowlisted global control labels are emitted.
    """

    if not config.is_production_like or not config.is_cloud:
        raise ConfigurationError("production telemetry requires staging/prod cloud mode")
    return Runtime(
        telemetry=TelemetryConfig(
            logging=LoggingConfig(
                filter=TelemetryFilter(core_level="WARN", other_level="ERROR")
            ),
            metrics=OpenTelemetryConfig(
                url=_LOCAL_OTLP_ENDPOINT,
                metric_periodicity=timedelta(seconds=30),
                durations_as_seconds=True,
            ),
            global_tags={
                "environment": config.env,
                "build_id": config.build_id,
                "worker_role": config.worker_role or "bootstrap",
            },
            metric_prefix="pacha_temporal_",
        ),
        worker_heartbeat_interval=timedelta(seconds=60),
    )


__all__ = ["build_runtime_telemetry"]
