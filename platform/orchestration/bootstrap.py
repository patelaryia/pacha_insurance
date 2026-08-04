"""One-shot immutable Temporal Schedule bootstrap used by T09 deployment."""

from __future__ import annotations

import asyncio

from orchestration.client import build_temporal_client
from orchestration.config import TemporalConfig
from orchestration.observability import configure_control_logging
from orchestration.runtime import _dependencies, _install_temporal_bridge
from orchestration.schedules import bootstrap_schedules
from orchestration.starter import TemporalStarter
from orchestration.telemetry import build_runtime_telemetry


async def run() -> tuple[str, ...]:
    config = TemporalConfig.from_environ()
    configure_control_logging(config)
    telemetry = build_runtime_telemetry(config)
    dependencies = _dependencies(config)
    client = await build_temporal_client(config, runtime=telemetry)
    _install_temporal_bridge(dependencies.app, TemporalStarter(client, config))
    graph = getattr(dependencies.app.state, "graph_integration", None)
    return await bootstrap_schedules(
        client,
        env=config.env,
        weekly_time="pack weekly",
        graph_service=graph,
        task_queue=config.task_queue("control"),
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()


__all__ = ["main", "run"]
