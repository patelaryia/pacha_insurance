"""Outbound-only RPA runner (PACKET-21 §7/§9).

This package is a *client*. It opens no listener, exposes no port, mounts no
callback server, and persists nothing durable: it pulls authorised work from the
platform control API, drives one isolated browser context, uploads evidence, and
posts a closed result. Everything it needs — the click path, the decrypted
payload, and the lease token — arrives once, over TLS, in the claim response.
"""

from __future__ import annotations

from projection_agent.runner.browser import (
    BrowserSession,
    ExactExecutor,
    SelectorDrift,
    StepFrame,
)
from projection_agent.runner.client import ControlPlane, RunnerClient
from projection_agent.runner.target_adapters import (
    TargetAdapter,
    TargetRegistry,
    load_target_registry,
)

__all__ = [
    "BrowserSession",
    "ControlPlane",
    "ExactExecutor",
    "RunnerClient",
    "SelectorDrift",
    "StepFrame",
    "TargetAdapter",
    "TargetRegistry",
    "load_target_registry",
]
