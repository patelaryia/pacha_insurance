"""Boundary coverage for Packet-03 engine modules."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock

import pytest
from sqlalchemy import event as sqlalchemy_event

from claim_core.app import create_app
from claim_core.calendars import Duration, add_duration
from claim_core.crypto import (
    LocalKeyProvider,
    decrypt_value,
    encrypt_value,
    normalise_blind_index,
)
from claim_core.ledger import canonical_json
from claim_core.schemas import ClaimCreate
from claim_core.storage import LocalBlobStore

AGENT = {"X-Actor": "agent:intake"}


def test_business_day_duration_skips_fixed_holiday_and_weekend() -> None:
    started = datetime(2026, 4, 30, 9, tzinfo=UTC)
    result = add_duration(started, Duration(1, "d"), "business", frozenset({"05-01"}))
    assert result == datetime(2026, 5, 4, 9, tzinfo=UTC)


def test_calendar_time_and_blocked_business_hours() -> None:
    started = datetime(2026, 7, 15, 9, tzinfo=UTC)
    assert add_duration(started, Duration(30, "m"), "24x7", frozenset()) == datetime(
        2026, 7, 15, 9, 30, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="blocked_on_inputs"):
        add_duration(started, Duration(24, "h"), "business", frozenset())


def test_envelope_round_trip_and_blind_index_normalisation() -> None:
    provider = LocalKeyProvider(b"m" * 32, b"i" * 32)
    dek = provider.generate_dek()
    wrapped = provider.wrap(dek)
    assert provider.unwrap(wrapped) == dek
    encrypted = encrypt_value({"name": "Wanjiku"}, dek)
    assert decrypt_value(encrypted, dek) == {"name": "Wanjiku"}
    assert normalise_blind_index("parties.insured.phone", "0722 000-111") == (
        "254722000111"
    )
    assert normalise_blind_index("parties.insured.kra_pin", " a-1 23 ") == "A123"
    assert len(provider.index_hmac("A123")) == 64


def test_local_blob_store_rejects_path_escape(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("documents/one", b"payload")
    assert store.get("documents/one") == b"payload"
    with pytest.raises(ValueError, match="escapes"):
        store.put("../outside", b"payload")


def test_ledger_genesis_single_row_chain_and_anchor(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    app = create_app("sqlite://", blob_store=store)
    assert app.state.ledger.verify_chain() == {
        "ok": True,
        "checked": 0,
        "first_bad_seq": None,
    }
    service = app.state.claim_service
    claim = service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.3.0"),
        AGENT["X-Actor"],
    )
    assert claim.id
    assert app.state.dispatcher.dispatch_once({"ledger"}) == 1
    assert app.state.ledger.verify_chain() == {
        "ok": True,
        "checked": 1,
        "first_bad_seq": None,
    }
    anchor = app.state.ledger.anchor_head()
    raw = store.get(f"audit-anchors/{anchor['date']}.json").decode()
    assert canonical_json(anchor) == raw


def test_idle_dispatch_prefilters_terminal_history_and_late_consumer_replays(
    tmp_path: Path,
) -> None:
    app = create_app(f"sqlite:///{tmp_path}/dispatch.db")
    seen: list[str] = []
    app.state.dispatcher.register_consumer("first", lambda event: seen.append(event.id))

    for _ in range(40):
        app.state.claim_service.create_claim(
            ClaimCreate(lob="motor", pack_version="motor@1.3.0"),
            AGENT["X-Actor"],
        )

    assert app.state.dispatcher.dispatch_once({"first"}) == 40
    assert len(seen) == 40

    statements: list[str] = []

    def capture_statement(*args) -> None:  # noqa: ANN002 - SQLAlchemy event contract
        statements.append(args[2])

    sqlalchemy_event.listen(app.state.engine, "before_cursor_execute", capture_statement)
    try:
        assert app.state.dispatcher.dispatch_once({"first"}) == 0
    finally:
        sqlalchemy_event.remove(app.state.engine, "before_cursor_execute", capture_statement)

    assert len(statements) <= 2

    replayed: list[str] = []
    app.state.dispatcher.register_consumer(
        "late",
        lambda event: replayed.append(event.id),
    )
    assert app.state.dispatcher.dispatch_once({"late"}) == 40
    assert replayed == seen


@pytest.mark.postgres_required
def test_concurrent_postgres_dispatchers_claim_one_delivery() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL locking contract")

    first = create_app(url)
    second = create_app(url)
    seen: list[str] = []
    seen_lock = Lock()
    start = Barrier(2)

    def consume(event) -> None:  # noqa: ANN001 - outbox consumer contract
        with seen_lock:
            seen.append(event.id)

    first.state.dispatcher.register_consumer("race", consume)
    second.state.dispatcher.register_consumer("race", consume)
    first.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.3.0"),
        AGENT["X-Actor"],
    )

    def dispatch(app) -> int:  # noqa: ANN001 - FastAPI instance
        start.wait()
        return app.state.dispatcher.dispatch_once({"race"})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, (first, second)))
    finally:
        first.state.engine.dispose()
        second.state.engine.dispose()

    assert sum(results) == 1
    assert len(seen) == 1
