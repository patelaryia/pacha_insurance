"""Database construction and transaction portability helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from claim_core.models import Base


class ClaimLocks:
    """Process-local serial transaction fallback used only for SQLite."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._lock = Lock()

    @contextmanager
    def acquire(self, _claim_id: str) -> Iterator[None]:
        if not self._enabled:
            yield
            return
        with self._lock:
            yield


def build_engine(database_url: str) -> Engine:
    """Create an engine with SQLite foreign-key enforcement enabled."""

    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def initialise_database(engine: Engine) -> None:
    """Create the Packet-1 schema for an empty local or test database."""

    # Optional packages share Base but own their build-time table activation.
    # Import order must not make create_app() silently install a later packet.
    optional_tables = {
        "agent_runs",
        "chase_checklists",
        "chase_items",
        "note_drafts",
        "savings_ledger",
        "vendors",
    }
    core_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in optional_tables
    ]
    Base.metadata.create_all(engine, tables=core_tables)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE events ALTER COLUMN seq SET DEFAULT "
                    "nextval('events_seq_seq')"
                )
            )


def build_session_factory(engine: Engine) -> sessionmaker:
    """Return the package's non-expiring SQLAlchemy session factory."""

    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def acquire_database_claim_lock(session: Session, claim_id: str) -> None:
    """Acquire the binding PostgreSQL transaction-scoped advisory lock."""

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:claim_id))"),
            {"claim_id": claim_id},
        )
