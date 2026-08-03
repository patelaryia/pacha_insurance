"""Focused unit coverage for PACKET-12 notification scheduling."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from orchestration.schedules import schedule_definitions


def test_digest_temporal_schedule_fires_at_0800_eat():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "packs/motor/notify/notify.yaml").read_text(
            encoding="utf-8"
        )
    )
    definitions = schedule_definitions(env="test", weekly_time="pack weekly")
    digest = next(item for item in definitions if item.workflow_name == "NotifyDigestWorkflow")
    assert digest.timing == "05:00 UTC daily"
    assert digest.overlap_policy.name == "BUFFER_ONE"
    assert digest.catchup_window == "24h"
    assert digest.pause_on_failure is False

    assert datetime(2026, 7, 15, 5, 0, tzinfo=UTC).astimezone(
        ZoneInfo(config["digest"]["timezone"])
    ).hour == 8
