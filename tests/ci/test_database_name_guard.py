"""Meta-tests: the only thing standing between a typo and a dropped database.

`tests/conftest.py` issues CREATE DATABASE, DROP DATABASE, and TRUNCATE against
whatever `DATABASE_URL` names. The naming rule in `support.database` is the
whole guard, so every accept and every reject is pinned here.
"""
from __future__ import annotations

import pytest

from support.database import (
    UnsafeDatabaseName,
    admin_url,
    database_name,
    is_base_name,
    is_droppable_name,
    isolated_database_name,
    require_base_name,
    require_droppable_name,
    with_database,
    worker_database_name,
)

# --- what counts as the configured test database ------------------------------

@pytest.mark.parametrize("name", ["pacha_test", "x_test", "pacha_claims_test"])
def test_accepts_names_ending_in_exactly_test(name):
    assert is_base_name(name) is True
    assert require_base_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "pacha",  # no suffix at all
        "pacha_testing",  # test-shaped, not a test database
        "pacha_test_prod",  # production hiding behind a prefix
        "pacha_tests",  # plural is a different database
        "test",  # bare, no base
        "_test",  # no identifier before the suffix
        "pacha_TEST",  # case matters; this is another database
        "postgres",  # the maintenance database is never a target
        "",
    ],
)
def test_rejects_anything_else(name):
    assert is_base_name(name) is False
    with pytest.raises(UnsafeDatabaseName):
        require_base_name(name)


# --- derived names ------------------------------------------------------------

def test_worker_name_is_the_base_plus_the_worker_id():
    assert worker_database_name("pacha_test", "gw0") == "pacha_test_gw0"
    assert worker_database_name("pacha_test", "gw11") == "pacha_test_gw11"


@pytest.mark.parametrize("worker_id", ["", "master"])
def test_serial_runs_keep_the_configured_database(worker_id):
    assert worker_database_name("pacha_test", worker_id) == "pacha_test"


@pytest.mark.parametrize("worker_id", ["gw", "0", "gw0; DROP DATABASE pacha", "../pacha"])
def test_unrecognised_worker_ids_are_refused(worker_id):
    with pytest.raises(UnsafeDatabaseName):
        worker_database_name("pacha_test", worker_id)


def test_worker_name_refuses_a_non_test_base():
    with pytest.raises(UnsafeDatabaseName):
        worker_database_name("pacha", "gw0")


def test_isolated_name_carries_a_hex_token():
    assert isolated_database_name("pacha_test", "a1b2c3") == "pacha_test_iso_a1b2c3"


@pytest.mark.parametrize("token", ["", "NOTHEX", "a1b2-c3", "a1b2 c3"])
def test_isolated_name_refuses_a_non_hex_token(token):
    with pytest.raises(UnsafeDatabaseName):
        isolated_database_name("pacha_test", token)


# --- what may be dropped ------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["pacha_test_gw0", "pacha_test_gw12", "pacha_test_iso_a1b2c3d4e5f6"]
)
def test_derived_databases_may_be_dropped(name):
    assert is_droppable_name(name) is True
    assert require_droppable_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "pacha_test",  # the configured base is reset, never dropped
        "pacha",
        "pacha_testing",
        "pacha_test_prod",
        "postgres",
        "pacha_test_gw",  # no worker number
        "pacha_test_iso_",  # no token
        "pacha_test_iso_zzz",  # token is not hex
        "",
    ],
)
def test_everything_else_is_refused_for_drop(name):
    assert is_droppable_name(name) is False
    with pytest.raises(UnsafeDatabaseName):
        require_droppable_name(name)


def test_the_base_is_never_droppable_even_though_it_is_a_valid_base():
    """Resetting the configured database is fine; dropping it is not."""
    assert is_base_name("pacha_test") is True
    assert is_droppable_name("pacha_test") is False


# --- URL surgery --------------------------------------------------------------

def test_database_name_reads_the_url_path():
    assert database_name("postgresql+psycopg://u:p@host:5432/pacha_test") == "pacha_test"


def test_with_database_preserves_driver_credentials_and_port():
    url = with_database("postgresql+psycopg://u:p@host:5432/pacha_test", "pacha_test_gw1")
    assert url == "postgresql+psycopg://u:p@host:5432/pacha_test_gw1"


def test_admin_url_points_at_the_maintenance_database():
    url = admin_url("postgresql+psycopg://u:p@host:5432/pacha_test")
    assert url == "postgresql+psycopg://u:p@host:5432/postgres"
