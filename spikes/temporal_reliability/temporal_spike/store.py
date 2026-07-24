"""Pacha-side authoritative store used by the isolated synthetic spike."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class AuthoritativeStore:
    """Small SQL store proving that claim facts and outcomes stay outside history.

    SQLite keeps the local spike dependency-free. The cloud acceptance report
    remains blocked until the same trial runs against an approved PostgreSQL
    database; this class is not a production persistence implementation.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS claims (
              claim_ref TEXT PRIMARY KEY,
              customer_name TEXT NOT NULL,
              bank_account TEXT NOT NULL,
              target_payload TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
              run_ref TEXT PRIMARY KEY,
              claim_ref TEXT NOT NULL,
              status TEXT NOT NULL,
              last_step TEXT NOT NULL,
              workflow_event_refs TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS run_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_ref TEXT NOT NULL,
              event_type TEXT NOT NULL,
              detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
              review_event_ref TEXT PRIMARY KEY,
              decision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS external_actions (
              write_id TEXT PRIMARY KEY,
              run_ref TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              receipt_ref TEXT
            );
            """
        )
        self._connection.commit()

    def seed_claim(
        self,
        claim_ref: str,
        *,
        customer_name: str,
        bank_account: str,
        target_payload: dict[str, Any],
    ) -> str:
        raw = json.dumps(target_payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO claims
                  (claim_ref, customer_name, bank_account, target_payload, status)
                VALUES (?, ?, ?, ?, 'INTIMATED')
                """,
                (claim_ref, customer_name, bank_account, raw),
            )
            self._connection.commit()
        return hashlib.sha256(raw.encode()).hexdigest()

    def claim(self, claim_ref: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM claims WHERE claim_ref = ?",
                (claim_ref,),
            ).fetchone()
        if row is None:
            raise KeyError(claim_ref)
        return dict(row)

    def target_payload(self, claim_ref: str) -> dict[str, Any]:
        return json.loads(self.claim(claim_ref)["target_payload"])

    def upsert_run(self, run_ref: str, claim_ref: str, status: str, step: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_runs (run_ref, claim_ref, status, last_step)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_ref) DO UPDATE SET
                  status = excluded.status,
                  last_step = excluded.last_step
                """,
                (run_ref, claim_ref, status, step),
            )
            self._connection.commit()

    def run(self, run_ref: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_runs WHERE run_ref = ?",
                (run_ref,),
            ).fetchone()
        if row is None:
            raise KeyError(run_ref)
        return dict(row)

    def event(self, run_ref: str, event_type: str, detail: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO run_events (run_ref, event_type, detail) VALUES (?, ?, ?)",
                (
                    run_ref,
                    event_type,
                    json.dumps(detail, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._connection.commit()

    def event_count(self, run_ref: str, event_type: str) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM run_events
                WHERE run_ref = ? AND event_type = ?
                """,
                (run_ref, event_type),
            ).fetchone()
        return int(row["count"])

    def review(self, review_event_ref: str, decision: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO reviews (review_event_ref, decision) VALUES (?, ?)",
                (review_event_ref, decision),
            )
            self._connection.commit()

    def review_decision(self, review_event_ref: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT decision FROM reviews WHERE review_event_ref = ?",
                (review_event_ref,),
            ).fetchone()
        if row is None:
            raise KeyError(review_event_ref)
        return str(row["decision"])

    def external_action(self, write_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM external_actions WHERE write_id = ?",
                (write_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_external_attempt(
        self,
        *,
        write_id: str,
        run_ref: str,
        payload_hash: str,
        status: str,
        receipt_ref: str | None,
    ) -> None:
        with self._lock:
            existing = self.external_action(write_id)
            attempts = 1 if existing is None else int(existing["attempts"]) + 1
            self._connection.execute(
                """
                INSERT INTO external_actions
                  (write_id, run_ref, payload_hash, status, attempts, receipt_ref)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(write_id) DO UPDATE SET
                  status = excluded.status,
                  attempts = excluded.attempts,
                  receipt_ref = excluded.receipt_ref
                """,
                (write_id, run_ref, payload_hash, status, attempts, receipt_ref),
            )
            self._connection.commit()

    def complete_claim(self, claim_ref: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE claims SET status = 'COMPLETED' WHERE claim_ref = ?",
                (claim_ref,),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
