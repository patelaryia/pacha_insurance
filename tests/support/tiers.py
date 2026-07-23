"""Central, reviewable selection policy for the pull-request PostgreSQL tier."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path

# Acceptance scenarios are selected by directory. These unit modules add
# database contracts whose assertions depend on migrations, transactional
# persistence, restart/recovery, or database-enforced constraints.
POSTGRES_REQUIRED_UNIT_MODULES = frozenset(
    {
        "test_agent_runtime_packet13.py",
        "test_approval_pack_packet18.py",
        "test_assessment_regressions.py",
        "test_assessment_review_fixes.py",
        "test_chase_regressions.py",
        "test_claim_core.py",
        "test_claim_core_packet03.py",
        "test_packet05_cto_regressions.py",
        "test_packet14_review_regressions.py",
        "test_review_queue_packet10.py",
    }
)


def requires_postgres(path: Path, marker_names: Set[str]) -> bool:
    """Return whether one collected test belongs in the required PG tier."""

    if "schema_isolated" in marker_names:
        return True
    parts = path.parts
    if "tests" in parts and "acceptance" in parts:
        return True
    return (
        "tests" in parts
        and "unit" in parts
        and path.name in POSTGRES_REQUIRED_UNIT_MODULES
    )
