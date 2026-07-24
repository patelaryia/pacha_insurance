"""Pacha's shared Temporal boundary (ADR-001, master plan T01).

Temporal owns orchestration only: Workflow position and recovery, durable
timers, technical retries, Activity heartbeats and human-input waits. It is
never the claim database, the business event ledger, the approval authority or
the record of an external write. Claim reads must keep working when Temporal
does not.

Other packages import only the names below. They must not import `temporalio`
directly except inside their own `workflows.py` and `activities.py`, which is
what keeps the configuration, encryption and payload rules in one reviewable
place instead of spread across every agent.

**Exports resolve lazily, and that is load-bearing.** The Workflow sandbox
passes through only the four deterministic modules named in
`worker.WORKFLOW_SAFE_MODULES`. Importing any of them imports this package
first, so an eager `__init__` would pull the client, the Codec and the
configuration — credentials, KMS calls, `os.urandom` — straight back into the
sandbox behind the narrow list. Deferring the imports keeps importing
`orchestration` free of side effects.

T02 adds `TemporalStarter` and T07 adds `bootstrap_schedules`. Neither is
exported before its packet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "ControlResult",
    "TemporalConfig",
    "WorkflowRef",
    "build_data_converter",
    "build_temporal_client",
    "build_worker",
]

_EXPORTS: dict[str, str] = {
    "ControlResult": "orchestration.contracts",
    "TemporalConfig": "orchestration.config",
    "WorkflowRef": "orchestration.ids",
    "build_data_converter": "orchestration.codec",
    "build_temporal_client": "orchestration.client",
    "build_worker": "orchestration.worker",
}

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from orchestration.client import build_temporal_client
    from orchestration.codec import build_data_converter
    from orchestration.config import TemporalConfig
    from orchestration.contracts import ControlResult
    from orchestration.ids import WorkflowRef
    from orchestration.worker import build_worker


def __getattr__(name: str) -> Any:
    """Resolve one of the six public names on first use."""

    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'orchestration' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
