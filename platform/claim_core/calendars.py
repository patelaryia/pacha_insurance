"""SLA calendar arithmetic defined by Packet-03 data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Duration:
    amount: int
    unit: str


def load_fixed_holidays(path: str | Path) -> frozenset[str]:
    """Load annually recurring fixed-date holidays as ``MM-DD`` values."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return frozenset(str(item) for item in payload.get("fixed_dates", []))


def is_business_day(day: date, fixed_holidays: frozenset[str]) -> bool:
    return day.weekday() < 5 and day.strftime("%m-%d") not in fixed_holidays


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def add_duration(
    started_at: datetime,
    duration: Duration,
    calendar: str,
    fixed_holidays: frozenset[str],
) -> datetime:
    """Add a duration under a registered SLA calendar."""

    started_at = _aware(started_at)
    if calendar in {"24x7", "send_window"}:
        delta = {
            "m": timedelta(minutes=duration.amount),
            "h": timedelta(hours=duration.amount),
            "d": timedelta(days=duration.amount),
        }.get(duration.unit)
        if delta is None:
            raise ValueError(f"unsupported duration unit {duration.unit!r}")
        return started_at + delta
    if calendar != "business":
        raise ValueError(f"unsupported SLA calendar {calendar!r}")
    if duration.unit != "d":
        raise ValueError("business-hour bounds are blocked_on_inputs")
    result = started_at
    remaining = duration.amount
    while remaining:
        result += timedelta(days=1)
        if is_business_day(result.date(), fixed_holidays):
            remaining -= 1
    return result
