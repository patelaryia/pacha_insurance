"""Assertions for the workflow-history privacy contract."""

from __future__ import annotations

from temporalio.client import WorkflowHistory

from .codec import ENCODING


def assert_history_has_no_plaintext(
    history: WorkflowHistory,
    forbidden_values: tuple[str, ...],
) -> None:
    raw = b"".join(event.SerializeToString() for event in history.events)
    for value in forbidden_values:
        if value.encode() in raw:
            raise AssertionError(f"workflow history contains forbidden plaintext: {value!r}")
    if ENCODING not in raw:
        raise AssertionError("workflow history contains no encrypted payload marker")
