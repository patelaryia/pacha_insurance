"""Synthetic PRD-09 target, click path, and runner identity — tests only.

Nothing here is production configuration. The production motor pack stays
`pending_capture`/`blocked_on_inputs` on every ICON and EDMS row, and the
deployment target registry stays fail-closed. These fixtures exist so the
control plane can be proved end to end without pretending an ICON or EDMS path
has been captured, a service account exists, or a target system is reachable.

A green Packet-21 build reports `synthetic_control_plane_green`, never
`rpa_live`.
"""

from __future__ import annotations

from fixtures.projection.click_path import (
    EDMS_CLAIMS_WORKFLOW,
    EDMS_FIELD_PATHS,
    EDMS_LIVE_ROW,
    FOLDER_REF,
    SEED_VALUES,
)
from fixtures.projection.runner import (
    FixtureRunnerAuthenticator,
    build_fixture_adapter,
)
from fixtures.projection.target import SyntheticSession, SyntheticTarget

__all__ = [
    "EDMS_CLAIMS_WORKFLOW",
    "EDMS_FIELD_PATHS",
    "EDMS_LIVE_ROW",
    "FOLDER_REF",
    "FixtureRunnerAuthenticator",
    "SEED_VALUES",
    "SyntheticSession",
    "SyntheticTarget",
    "build_fixture_adapter",
]
