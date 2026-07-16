"""Focused fail-closed tests for the PRD-06 public builder inputs."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chase_agent import _load_config, _load_registry
from chase_agent.api import WaiveRequest


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "body",
    [
        "version: 2\nitems: {}\n",
        "version: 1\nitems: {broken: value}\n",
        "version: 1\nitems: {broken: {kind: unknown, physical: false}}\n",
        "version: 1\nitems: {broken: {kind: physical, physical: false}}\n",
        "version: 1\nitems: {broken: {kind: document, physical: false}}\n",
        "version: 1\nitems: {broken: {kind: field_request, physical: false}}\n",
        (
            "version: 1\nitems: {broken: {kind: physical, physical: true, "
            "unexpected: true}}\n"
        ),
    ],
)
def test_registry_rejects_values_outside_the_closed_contract(tmp_path, body):
    with pytest.raises(ValueError):
        _load_registry(_write(tmp_path / "items.yaml", body))


def test_chase_config_rejects_an_override_of_binding_cadence(tmp_path):
    path = _write(
        tmp_path / "chase.yaml",
        """version: 1
cadence_days: [3, 7, 12]
repeat_days: 7
reminder_cap: 6
inbound_defer: {window_hours: 24, defer_hours: 48}
cc_insured_from_reminder: 2
reject_reasons: {illegible: unreadable}
""",
    )
    with pytest.raises(ValueError, match="launch contract"):
        _load_config(path, {"reminder_cap": 7})


def test_waiver_reason_cannot_be_whitespace():
    with pytest.raises(ValidationError):
        WaiveRequest(reason="   ")
