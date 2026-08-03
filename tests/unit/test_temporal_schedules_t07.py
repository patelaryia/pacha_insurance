"""Substantive TEMPORAL-T07 bootstrap and sandbox tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from temporalio.service import RPCError, RPCStatusCode

from orchestration.schedules import RECURRING_WORKFLOWS, bootstrap_schedules
from orchestration.worker import default_workflow_runner


class FakeHandle:
    def __init__(self, client: FakeScheduleClient, schedule_id: str) -> None:
        self.client = client
        self.schedule_id = schedule_id

    async def describe(self):
        if self.schedule_id not in self.client.schedules:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return SimpleNamespace(schedule=self.client.schedules[self.schedule_id])


class FakeScheduleClient:
    def __init__(self) -> None:
        self.schedules: dict[str, object] = {}

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        return FakeHandle(self, schedule_id)

    async def create_schedule(self, schedule_id: str, schedule: object) -> None:
        self.schedules[schedule_id] = schedule


def _graph():
    return SimpleNamespace(
        inbound=SimpleNamespace(delta_once=lambda: None, renew_once=lambda: None),
        outbound=SimpleNamespace(release_due=lambda _now: None),
    )


def test_bootstrap_creates_once_then_compares_without_mutating():
    asyncio.run(_bootstrap_scenario())


async def _bootstrap_scenario() -> None:
    client = FakeScheduleClient()
    first = await bootstrap_schedules(
        client,
        env="test",
        weekly_time="pack weekly",
        graph_service=_graph(),
    )
    assert len(first) == 9
    assert await bootstrap_schedules(
        client,
        env="test",
        weekly_time="pack weekly",
        graph_service=_graph(),
    ) == ()

    changed = next(iter(client.schedules))
    client.schedules[changed] = object()
    with pytest.raises(RuntimeError, match="schedule definition drift"):
        await bootstrap_schedules(
            client,
            env="test",
            weekly_time="pack weekly",
            graph_service=_graph(),
        )


def test_every_recurring_workflow_prepares_in_the_production_sandbox():
    asyncio.run(_prepare_workflows())


async def _prepare_workflows() -> None:
    runner = default_workflow_runner()
    for workflow_class in RECURRING_WORKFLOWS:
        runner.prepare_workflow(workflow_class.__temporal_workflow_definition)
