"""Cross-database test isolation shared by acceptance and unit suites.

SQLite tests get a fresh file per test from `tmp_path` and need nothing here.

PostgreSQL is the expensive one. It used to `DROP SCHEMA public CASCADE` before
every test, which meant every application table was rebuilt from scratch
hundreds of times per run, and it could not be parallelised at all: two workers
sharing one database would drop each other's tables mid-test.

Instead:

* each xdist worker gets its own database, built once at session start;
* ordinary tests are isolated by `TRUNCATE ... RESTART IDENTITY CASCADE`, which
  leaves the schema in place;
* the repeated `MetaData.create_all` that every `build_*` call makes is skipped
  once the session has already created that table on the shared worker
  database — and only there;
* tests marked `schema_isolated` opt out entirely. They assert on the schema
  itself or run Alembic, so they get a private empty database and the normal
  `create_all`/migration behaviour.

Every database this module creates, resets, or drops has its name checked by
`support.database` first. See that module for the naming rule.
"""
from __future__ import annotations

import hashlib
import os

import pytest
from sqlalchemy import MetaData, create_engine

from support.database import (
    admin_url,
    database_name,
    isolated_database_name,
    require_base_name,
    require_droppable_name,
    with_database,
)
from support.tiers import requires_postgres

_UNPATCHED_CREATE_ALL = MetaData.create_all


def pytest_collection_modifyitems(items) -> None:  # noqa: ANN001 - pytest hook
    """Apply the auditable PostgreSQL tier policy before marker selection."""

    for item in items:
        marker_names = {marker.name for marker in item.iter_markers()}
        if requires_postgres(item.path, marker_names):
            item.add_marker(pytest.mark.postgres_required)


def _is_postgresql(url: str | None) -> bool:
    return url is not None and url.startswith("postgresql")


def _import_every_model() -> MetaData:
    """Every package that declares tables, so one `create_all` builds them all.

    Optional packages are registered explicitly rather than discovered: a
    package that is silently missed would have its tables created lazily by the
    first `build_*` call, which the suppression below would then skip.
    """
    import agent_runtime  # noqa: F401
    import chase_agent  # noqa: F401
    import cop_runtime  # noqa: F401
    import eval_harness  # noqa: F401
    import intake_agent  # noqa: F401
    import notify  # noqa: F401
    import projection_agent  # noqa: F401
    import review_queue  # noqa: F401
    from claim_core.models import Base
    from doc_intel import engine as _doc_intel_engine  # noqa: F401

    return Base.metadata


def _admin_engine(url: str):
    return create_engine(admin_url(url), isolation_level="AUTOCOMMIT")


def _quote(engine, identifier: str) -> str:
    return engine.dialect.identifier_preparer.quote(identifier)


def _recreate_database(url: str, name: str) -> None:
    """Drop and rebuild one derived database, through the admin database."""

    require_droppable_name(name)
    engine = _admin_engine(url)
    try:
        with engine.connect() as connection:
            quoted = _quote(engine, name)
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
            connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
    finally:
        engine.dispose()


def _drop_database(url: str, name: str) -> None:
    require_droppable_name(name)
    engine = _admin_engine(url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                f"DROP DATABASE IF EXISTS {_quote(engine, name)} WITH (FORCE)"
            )
    finally:
        engine.dispose()


def _reset_public_schema(url: str) -> None:
    """Serial runs reuse the configured database, so empty it once at start."""

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()


def _application_tables(engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return [row[0] for row in rows]


def _truncate_all(engine) -> None:
    tables = _application_tables(engine)
    if not tables:
        return
    targets = ", ".join(_quote(engine, table) for table in tables)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE TABLE {targets} RESTART IDENTITY CASCADE")


def _install_create_all_suppression(monkeypatch, database: str, created: set[str]) -> None:
    """Skip `create_all` for tables this session already built — here only.

    Falls through to the real implementation for anything unrecognised, so a
    package whose models were not registered at session start still works; it
    just pays the reflection round-trip it always did.
    """

    def create_all(self, bind=None, tables=None, checkfirst=True):  # noqa: ANN001
        target = getattr(getattr(bind, "url", None), "database", None)
        if target == database:
            requested = (
                {table.name for table in tables}
                if tables is not None
                else set(self.tables)
            )
            if requested and requested <= created:
                return None
        return _UNPATCHED_CREATE_ALL(self, bind=bind, tables=tables, checkfirst=checkfirst)

    monkeypatch.setattr(MetaData, "create_all", create_all)


@pytest.fixture(scope="session")
def postgresql_session(request):
    """Build this worker's database once, or yield None on SQLite."""

    configured_url = os.environ.get("DATABASE_URL")
    if not _is_postgresql(configured_url):
        yield None
        return

    base = require_base_name(database_name(configured_url))
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "master")

    if worker_id in {"", "master"}:
        worker_name = base
        worker_url = configured_url
        _reset_public_schema(worker_url)
    else:
        from support.database import worker_database_name

        worker_name = worker_database_name(base, worker_id)
        worker_url = with_database(configured_url, worker_name)
        _recreate_database(configured_url, worker_name)

    metadata = _import_every_model()
    engine = create_engine(worker_url)
    _UNPATCHED_CREATE_ALL(metadata, bind=engine)
    created = set(_application_tables(engine))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", worker_url)
    _install_create_all_suppression(monkeypatch, worker_name, created)
    try:
        yield {
            "configured_url": configured_url,
            "base": base,
            "worker_url": worker_url,
            "engine": engine,
        }
    finally:
        monkeypatch.undo()
        engine.dispose()
        if worker_name != base:
            _drop_database(configured_url, worker_name)


@pytest.fixture(autouse=True)
def isolate_postgresql_test_database(request, postgresql_session, monkeypatch):
    """Truncate before each test, or hand the test its own empty database."""

    if postgresql_session is None:
        yield
        return

    if request.node.get_closest_marker("schema_isolated") is None:
        _truncate_all(postgresql_session["engine"])
        yield
        return

    token = hashlib.blake2b(request.node.nodeid.encode("utf-8"), digest_size=6).hexdigest()
    name = isolated_database_name(postgresql_session["base"], token)
    configured_url = postgresql_session["configured_url"]
    _recreate_database(configured_url, name)
    monkeypatch.setenv("DATABASE_URL", with_database(configured_url, name))
    try:
        yield
    finally:
        monkeypatch.undo()
        _drop_database(configured_url, name)
