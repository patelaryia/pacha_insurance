"""Boundary coverage for Packet-03 engine modules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
