"""Focused unit coverage for PACKET-12 notification scheduling."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import yaml

from claim_core import celery_app
from notify.digest import configure_digest


def test_digest_beat_schedule_fires_at_0800_eat_without_global_timezone_change():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "packs/motor/notify/notify.yaml").read_text(
            encoding="utf-8"
        )
    )
    ledger_schedule = celery_app.conf.beat_schedule["verify-ledger-nightly"][
        "schedule"
    ]

    configure_digest(cast(Any, object()), config)

    entry = celery_app.conf.beat_schedule["notify-daily-digest"]
    schedule = entry["schedule"]
    assert celery_app.timezone == ZoneInfo("UTC")
    assert schedule.hour == {5}
    assert schedule.minute == {0}
    assert "options" not in entry
    assert ledger_schedule.hour == {1}

    schedule.nowfun = lambda: datetime(2026, 7, 15, 5, 0, tzinfo=UTC)
    due, _next = schedule.is_due(datetime(2026, 7, 14, 5, 0, tzinfo=UTC))
    assert due is True
    assert datetime(2026, 7, 15, 5, 0, tzinfo=UTC).astimezone(
        ZoneInfo(config["digest"]["timezone"])
    ).hour == 8
