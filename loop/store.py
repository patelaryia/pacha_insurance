#!/usr/bin/env python3
"""The durable ledger. SQLite in WAL mode, one row per packet, append-only events.

Authoritative runtime state lives here, not in packet files and not in agent
context. A complete snapshot and event stream are published to the
controller-only audit branch so state can be recovered without the controller
ever committing into a development checkout.

Two properties this file exists to provide, both of which the previous
design lacked:

- **Leases.** A packet in a running state is owned by exactly one process
  until its lease expires. A crashed controller does not strand a packet in
  `building` forever; the next tick reclaims the lease and records why.
- **Serialisation.** `BEGIN IMMEDIATE` on every write, so two controllers
  cannot both claim the same packet. SQLite's writer lock does the work
  that an advisory flock could not do across machines.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import socket
import sqlite3
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = REPO / "loop" / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id             TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    branch         TEXT NOT NULL,
    base_sha       TEXT,             -- pinned at dispatch; every attempt is against this
    head_sha       TEXT,             -- exact commit validated, reviewed and eligible to merge
    pr_number      INTEGER,
    effective_blast_radius INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    rework_cycles  INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 0,
    lease_owner    TEXT,             -- host:pid:uuid of the controller holding it
    lease_expires  REAL,             -- unix seconds; NULL when unleased
    reason         TEXT,             -- why it is blocked/escalated, in prose
    updated_at     REAL NOT NULL
);

-- Append-only. Never updated, never deleted. This is the answer to
-- "why did this merge", and it survives a lost packet file.
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id  TEXT NOT NULL,
    at         REAL NOT NULL,
    kind       TEXT NOT NULL,
    from_status TEXT,
    to_status  TEXT,
    payload    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id    TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    kind         TEXT NOT NULL,       -- build | rework
    base_sha     TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    outcome      TEXT,
    wall_seconds INTEGER,
    tokens       INTEGER,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS events_packet ON events(packet_id, seq);
CREATE INDEX IF NOT EXISTS attempts_packet ON attempts(packet_id, attempt);

CREATE TABLE IF NOT EXISTS controller_lock (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner          TEXT,
    expires        REAL
);
INSERT OR IGNORE INTO controller_lock (singleton, owner, expires)
VALUES (1, NULL, NULL);
"""

# The controller is the only thing that writes these. An agent returning a
# status is returning a recommendation; the controller decides.
STATUSES = frozenset(
    {
        "queued",        # dispatchable once dependencies are merged
        "building",      # leased, a builder is running
        "awaiting_ci",   # PR open, polling check runs
        "review",        # CI green, waiting on the reviewer
        "rework",        # reviewer or red CI sent it back; DISPATCHABLE
        "merge_ready",   # approved, green, routine — waiting on a merge
        "merged",        # reconciled from GitHub, not asserted locally
        "blocked",       # a breaker tripped; `reason` is required
        "escalated",     # needs a human decision; `reason` is required
    }
)

DISPATCHABLE = frozenset({"queued", "rework"})
LEASED = frozenset({"building"})
TERMINAL = frozenset({"merged", "blocked", "escalated"})
OPEN_ON_GITHUB = frozenset({"awaiting_ci", "review", "rework", "merge_ready"})


def owner_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def open_db(path: pathlib.Path | None = None) -> sqlite3.Connection:
    """A configured connection the caller owns. Used by tests, which need a
    connection whose lifetime is the fixture's, not a `with` block's."""
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextlib.contextmanager
def connect(path: pathlib.Path | None = None):
    conn = open_db(path)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def write(conn: sqlite3.Connection):
    """One serialised write transaction. BEGIN IMMEDIATE takes the writer
    lock up front, so a second controller blocks here rather than losing a
    race it did not know it was in."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # SQLite does not apply new columns from CREATE TABLE IF NOT EXISTS to an
    # existing ledger. Keep upgrades explicit and additive so installing a
    # controller update never requires deleting the authoritative state.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(packets)")}
    migrations = {
        "head_sha": "ALTER TABLE packets ADD COLUMN head_sha TEXT",
        "effective_blast_radius": (
            "ALTER TABLE packets ADD COLUMN effective_blast_radius "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "version": "ALTER TABLE packets ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
    }
    for name, statement in migrations.items():
        if name not in columns:
            conn.execute(statement)


# --- events ------------------------------------------------------------------


def record(conn, packet_id: str, kind: str, *, frm=None, to=None, **payload) -> None:
    conn.execute(
        "INSERT INTO events (packet_id, at, kind, from_status, to_status, payload)"
        " VALUES (?,?,?,?,?,?)",
        (packet_id, time.time(), kind, frm, to, json.dumps(payload, default=str)),
    )


def events(conn, packet_id: str | None = None, since_seq: int = 0) -> list[sqlite3.Row]:
    if packet_id:
        return conn.execute(
            "SELECT * FROM events WHERE packet_id=? AND seq>? ORDER BY seq", (packet_id, since_seq)
        ).fetchall()
    return conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (since_seq,)).fetchall()


# --- packets -----------------------------------------------------------------


def upsert_spec(
    conn,
    packet_id: str,
    branch: str,
    *,
    bootstrap_status: str = "queued",
    pr_number: int | None = None,
    attempts: int = 0,
    reason: str | None = None,
    declared_blast_radius: bool = False,
) -> None:
    """Register a packet the board declares but the ledger has not seen.

    `bootstrap_status` is read from the packet file's committed history
    and used ONLY when creating the row. Without it, first contact with a
    board holding 22 already-merged packets would resurrect all of them at
    `queued` and rebuild the entire product.

    A packet already in the ledger keeps its status. The board is spec, the
    ledger is state, and re-reading the spec must never move a packet.
    """
    existing = conn.execute(
        "SELECT * FROM packets WHERE id=?",
        (packet_id,),
    ).fetchone()
    if existing:
        effective_blast = max(
            existing["effective_blast_radius"],
            int(declared_blast_radius),
        )
        if existing["branch"] == branch and (
            existing["effective_blast_radius"] == effective_blast
        ):
            return
        conn.execute(
            "UPDATE packets SET branch=?,"
            " effective_blast_radius=?, version=version+1,"
            " updated_at=? WHERE id=?",
            (branch, effective_blast, time.time(), packet_id),
        )
        record(
            conn,
            packet_id,
            "spec_updated",
            branch=branch,
            effective_blast_radius=bool(effective_blast),
        )
        return
    status = bootstrap_status if bootstrap_status in STATUSES else "queued"
    conn.execute(
        "INSERT INTO packets"
        " (id, status, branch, pr_number, attempts, reason,"
        "  effective_blast_radius, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            packet_id,
            status,
            branch,
            pr_number,
            attempts,
            reason,
            int(declared_blast_radius),
            time.time(),
        ),
    )
    record(conn, packet_id, "registered", to=status, branch=branch, bootstrapped=True)


def get(conn, packet_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM packets WHERE id=?", (packet_id,)).fetchone()


def all_packets(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM packets ORDER BY id").fetchall()


def set_status(
    conn,
    packet_id: str,
    status: str,
    *,
    reason=None,
    kind="transition",
    expected_status: str | None = None,
    expected_version: int | None = None,
    **payload,
) -> bool:
    if status not in STATUSES:
        raise ValueError(f"{status!r} is not a status the controller knows")
    if status in ("blocked", "escalated") and not reason:
        raise ValueError(f"{status} requires a written reason — that is the whole point")
    row = get(conn, packet_id)
    if row is None:
        raise ValueError(f"no packet {packet_id} in the ledger")
    if expected_status is not None and row["status"] != expected_status:
        return False
    if expected_version is not None and row["version"] != expected_version:
        return False
    cursor = conn.execute(
        "UPDATE packets SET status=?, reason=?, version=version+1, updated_at=?"
        " WHERE id=? AND version=?",
        (status, reason, time.time(), packet_id, row["version"]),
    )
    if not cursor.rowcount:
        return False
    record(conn, packet_id, kind, frm=row["status"], to=status, reason=reason, **payload)
    return True


def set_fields(conn, packet_id: str, **fields) -> None:
    allowed = {
        "branch",
        "base_sha",
        "head_sha",
        "pr_number",
        "attempts",
        "rework_cycles",
        "reason",
        "effective_blast_radius",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot set {unknown} on a packet")
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE packets SET {assignments}, version=version+1, updated_at=? WHERE id=?",
        (*fields.values(), time.time(), packet_id),
    )


# --- leases ------------------------------------------------------------------


def acquire(conn, packet_id: str, owner: str, ttl_seconds: int) -> bool:
    """Claim a packet for this controller. False if someone else holds it.

    The UPDATE is conditional on the lease being free or expired, so the
    check and the claim are one atomic statement — a check followed by a
    separate write is the race this exists to avoid.
    """
    now = time.time()
    cursor = conn.execute(
        "UPDATE packets SET lease_owner=?, lease_expires=?, updated_at=?"
        " WHERE id=? AND (lease_owner IS NULL OR lease_expires < ?)",
        (owner, now + ttl_seconds, now, packet_id, now),
    )
    if cursor.rowcount:
        record(conn, packet_id, "lease_acquired", owner=owner, ttl=ttl_seconds)
        return True
    return False


def release(conn, packet_id: str, owner: str) -> None:
    conn.execute(
        "UPDATE packets SET lease_owner=NULL, lease_expires=NULL, updated_at=?"
        " WHERE id=? AND lease_owner=?",
        (time.time(), packet_id, owner),
    )
    record(conn, packet_id, "lease_released", owner=owner)


def renew(conn, packet_id: str, owner: str, ttl_seconds: int) -> bool:
    cursor = conn.execute(
        "UPDATE packets SET lease_expires=? WHERE id=? AND lease_owner=?",
        (time.time() + ttl_seconds, packet_id, owner),
    )
    return bool(cursor.rowcount)


def expired_leases(conn) -> list[sqlite3.Row]:
    """Packets whose owner died. Recovering these is the difference between
    a loop that survives a laptop lid closing and one that silently stops."""
    return conn.execute(
        "SELECT * FROM packets WHERE lease_owner IS NOT NULL AND lease_expires < ?",
        (time.time(),),
    ).fetchall()


# --- controller ownership ----------------------------------------------------


def acquire_controller(conn, owner: str, ttl_seconds: int) -> bool:
    """Acquire the one controller lease.

    Packet leases prevent duplicate builders. This lease protects every other
    external side effect too: CI folding, reviewer invocation, approval,
    merge and audit publication. Without it, two scheduled ticks can run two
    reviewers and apply stale, contradictory verdicts.
    """
    now = time.time()
    cursor = conn.execute(
        "UPDATE controller_lock SET owner=?, expires=?"
        " WHERE singleton=1 AND (owner IS NULL OR expires < ? OR owner=?)",
        (owner, now + ttl_seconds, now, owner),
    )
    return bool(cursor.rowcount)


def renew_controller(conn, owner: str, ttl_seconds: int) -> bool:
    cursor = conn.execute(
        "UPDATE controller_lock SET expires=? WHERE singleton=1 AND owner=?",
        (time.time() + ttl_seconds, owner),
    )
    return bool(cursor.rowcount)


def release_controller(conn, owner: str) -> None:
    conn.execute(
        "UPDATE controller_lock SET owner=NULL, expires=NULL"
        " WHERE singleton=1 AND owner=?",
        (owner,),
    )


# --- attempts ----------------------------------------------------------------


def start_attempt(conn, packet_id: str, attempt: int, kind: str, base_sha: str) -> int:
    cursor = conn.execute(
        "INSERT INTO attempts (packet_id, attempt, kind, base_sha, started_at)"
        " VALUES (?,?,?,?,?)",
        (packet_id, attempt, kind, base_sha, time.time()),
    )
    conn.execute(
        "UPDATE packets SET attempts=?, base_sha=?, updated_at=? WHERE id=?",
        (attempt, base_sha, time.time(), packet_id),
    )
    return cursor.lastrowid


def finish_attempt(conn, attempt_id: int, outcome: str, *, tokens=None, detail="") -> None:
    row = conn.execute("SELECT started_at FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    now = time.time()
    conn.execute(
        "UPDATE attempts SET ended_at=?, outcome=?, wall_seconds=?, tokens=?, detail=?"
        " WHERE id=?",
        (now, outcome, int(now - row["started_at"]), tokens, detail, attempt_id),
    )


def attempts_today(conn) -> dict:
    """Spend since midnight UTC, folded out of the attempts table."""
    midnight = time.time() - (time.time() % 86400)
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(wall_seconds),0) secs,"
        " SUM(CASE WHEN tokens IS NULL THEN 1 ELSE 0 END) unknown,"
        " COALESCE(SUM(tokens),0) tokens"
        " FROM attempts WHERE started_at >= ?",
        (midnight,),
    ).fetchone()
    return {
        "attempts": row["n"],
        "builder_minutes": row["secs"] // 60,
        # A partial token count is worse than none: it looks like a budget.
        "tokens": None if (row["unknown"] or not row["n"]) else row["tokens"],
    }


def recent_outcomes(conn, window: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.status, a.outcome FROM packets p"
        " JOIN attempts a ON a.id = ("
        "   SELECT id FROM attempts WHERE packet_id=p.id ORDER BY id DESC LIMIT 1)"
        " WHERE p.status IN ('merged','blocked','escalated')"
        " ORDER BY p.updated_at DESC LIMIT ?",
        (window,),
    ).fetchall()


# --- durable audit snapshot -------------------------------------------------


def export_state(conn: sqlite3.Connection) -> dict:
    """A complete, JSON-serialisable ledger snapshot for the audit branch."""
    return {
        "schema": 1,
        "packets": [dict(row) for row in conn.execute("SELECT * FROM packets ORDER BY id")],
        "attempts": [
            dict(row) for row in conn.execute("SELECT * FROM attempts ORDER BY id")
        ],
        "events": [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")],
    }


def restore_state(conn: sqlite3.Connection, snapshot: dict) -> None:
    """Restore an empty ledger from an audited snapshot.

    Refuse to merge two histories. Recovery is deliberately all-or-nothing so
    a stale backup cannot silently overwrite live controller state.
    """
    if snapshot.get("schema") != 1:
        raise ValueError("unsupported loop audit snapshot schema")
    if conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]:
        raise ValueError("refusing to restore over a non-empty loop ledger")
    packet_columns = [row["name"] for row in conn.execute("PRAGMA table_info(packets)")]
    attempt_columns = [row["name"] for row in conn.execute("PRAGMA table_info(attempts)")]
    event_columns = [row["name"] for row in conn.execute("PRAGMA table_info(events)")]
    with write(conn):
        for table, rows, columns in (
            ("packets", snapshot.get("packets") or [], packet_columns),
            ("attempts", snapshot.get("attempts") or [], attempt_columns),
            ("events", snapshot.get("events") or [], event_columns),
        ):
            for row in rows:
                names = [name for name in columns if name in row]
                marks = ",".join("?" for _ in names)
                conn.execute(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({marks})",
                    [row[name] for name in names],
                )
