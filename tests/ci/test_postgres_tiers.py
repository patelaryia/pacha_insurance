"""The required PostgreSQL tier cannot silently lose protected scenarios."""

from pathlib import Path

from support.tiers import POSTGRES_REQUIRED_UNIT_MODULES, requires_postgres


def test_every_acceptance_and_schema_isolated_test_requires_postgres() -> None:
    assert requires_postgres(
        Path("tests/acceptance/test_packet_17_assessment_cascade.py"),
        set(),
    )
    assert requires_postgres(
        Path("tests/unit/test_packet05_cto_regressions.py"),
        {"schema_isolated"},
    )


def test_database_contract_units_are_selected_but_pure_units_are_not() -> None:
    assert "test_claim_core.py" in POSTGRES_REQUIRED_UNIT_MODULES
    assert requires_postgres(Path("tests/unit/test_claim_core.py"), set())
    assert not requires_postgres(Path("tests/unit/test_doc_intel_raster_dpi.py"), set())
    assert not requires_postgres(Path("tests/ci/test_coverage_guard.py"), set())
