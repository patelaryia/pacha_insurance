"""Cross-database test isolation shared by acceptance and unit suites."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine


@pytest.fixture(autouse=True)
def isolate_postgresql_test_database():
    """Give PostgreSQL tests the same per-test isolation as SQLite tmp files."""

    database_url = os.environ.get("DATABASE_URL")
    if database_url is None or not database_url.startswith("postgresql"):
        yield
        return
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith("_test"):
        pytest.fail(
            "Refusing to reset PostgreSQL database without an '_test' suffix"
        )
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    yield
