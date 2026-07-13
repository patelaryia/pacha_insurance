"""Per-document duration/cost samples and individual-breach alerts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol


class AlertSink(Protocol):
    def alert(self, code: str, payload: dict[str, Any]) -> None: ...


class NullAlertSink:
    """Explicit test-only alert sink."""

    def alert(self, code: str, payload: dict[str, Any]) -> None:
        return None


class LoggingAlertSink:
    """Loud local-driver sink used when no external pager is configured."""

    def alert(self, code: str, payload: dict[str, Any]) -> None:
        logging.getLogger("pacha.doc_intel.alerts").error(
            "%s %s", code, payload
        )


class SloSentinel:
    def __init__(
        self,
        *,
        duration_limit_ms: int,
        cost_limit_usd: Decimal,
        sample_sink: Callable[[dict[str, Any]], None],
        alert_sink: AlertSink,
    ) -> None:
        self.duration_limit_ms = duration_limit_ms
        self.cost_limit_usd = cost_limit_usd
        self.sample_sink = sample_sink
        self.alert_sink = alert_sink

    def record(self, *, document_id: str, duration_ms: int, cost_usd: Decimal) -> dict[str, Any]:
        sample = {
            "document_id": document_id,
            "duration_ms": duration_ms,
            "cost_usd": cost_usd,
            "breached_duration": duration_ms > self.duration_limit_ms,
            "breached_cost": cost_usd > self.cost_limit_usd,
        }
        self.sample_sink(sample)
        if sample["breached_duration"]:
            self.alert_sink.alert("DOC_INTEL_DURATION_BREACH", dict(sample))
        if sample["breached_cost"]:
            self.alert_sink.alert("DOC_INTEL_COST_BREACH", dict(sample))
        return sample
