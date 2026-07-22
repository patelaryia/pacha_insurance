"""Database-name derivation and the guard that stands in front of DROP.

The PostgreSQL suite creates and drops databases. The only thing between a
mistyped `DATABASE_URL` and a dropped production database is the naming rule
below, so it is deliberately narrow, exact-match, and separately tested:

* the configured base must end in exactly `_test` — `pacha_testing` and
  `pacha_test_prod` are rejected, because "starts with something test-shaped"
  is not the same claim as "is a test database";
* the only names this suite will ever create or drop are that base plus one of
  the two derived suffixes, `_gw<n>` for an xdist worker and `_iso_<token>`
  for a `schema_isolated` test.

Anything else raises. No fallback, no "close enough".
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

#: The configured base database: any identifier ending in exactly `_test`.
BASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*_test$")

#: A base plus the one suffix this suite is allowed to derive and drop.
DERIVED_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*_test(?:_gw\d+|_iso_[0-9a-f]{1,32})$")

#: PostgreSQL's own maintenance database — never a target, only a connection point.
ADMIN_DATABASE = "postgres"


class UnsafeDatabaseName(RuntimeError):
    """A name that this suite refuses to create, reset, or drop."""


def is_base_name(name: str) -> bool:
    """Is this the configured test database itself?"""
    return bool(BASE_NAME.fullmatch(name))


def is_derived_name(name: str) -> bool:
    """Is this a worker or isolated database this suite derived?"""
    return bool(DERIVED_NAME.fullmatch(name))


def is_droppable_name(name: str) -> bool:
    """Only derived databases are ever dropped; the configured base is not."""
    return is_derived_name(name)


def require_base_name(name: str) -> str:
    """Return `name`, or refuse to touch the database it points at."""
    if not is_base_name(name):
        raise UnsafeDatabaseName(
            f"refusing to run the PostgreSQL suite against {name!r}: "
            "DATABASE_URL must name a database ending in exactly '_test'"
        )
    return name


def require_droppable_name(name: str) -> str:
    """Return `name`, or refuse to drop it."""
    if not is_droppable_name(name):
        raise UnsafeDatabaseName(
            f"refusing to drop {name!r}: only databases this session derived "
            "as '<base>_test_gw<n>' or '<base>_test_iso_<token>' may be dropped"
        )
    return name


def worker_database_name(base: str, worker_id: str) -> str:
    """`pacha_test` + `gw3` -> `pacha_test_gw3`; serial runs keep the base."""
    require_base_name(base)
    if worker_id in {"", "master"}:
        return base
    if not re.fullmatch(r"gw\d+", worker_id):
        raise UnsafeDatabaseName(f"unrecognised xdist worker id {worker_id!r}")
    return f"{base}_{worker_id}"


def isolated_database_name(base: str, token: str) -> str:
    """A private, empty database for one `schema_isolated` test."""
    require_base_name(base)
    if not re.fullmatch(r"[0-9a-f]{1,32}", token):
        raise UnsafeDatabaseName(f"isolated-database token must be lowercase hex, got {token!r}")
    return f"{base}_iso_{token}"


def database_name(url: str) -> str:
    """The database a URL points at, with no leading slash."""
    return urlparse(url).path.removeprefix("/")


def with_database(url: str, name: str) -> str:
    """The same URL, pointed at a different database."""
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{name}"))


def admin_url(url: str) -> str:
    """The same server, reached through the maintenance database.

    CREATE/DROP DATABASE cannot run while connected to the target, so every
    lifecycle statement goes through here.
    """
    return with_database(url, ADMIN_DATABASE)
