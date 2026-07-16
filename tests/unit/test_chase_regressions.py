"""Regression probes for PACKET-15 reviewer findings."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text


def _acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance/test_packet_15_chase_agent.py"
    spec = importlib.util.spec_from_file_location("packet15_acceptance_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P15 = _acceptance_module()


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def test_initial_request_retries_at_next_send_window(tmp_path):
    env = P15._build(tmp_path, "initial-window-retry", model=P15._intimation_model())
    env.clock.advance_to(P15.T0 - timedelta(days=1))  # Sunday, 12:00 EAT
    claim_id = P15._to_checklist(env)

    assert P15._drafts(env.app, claim_id, "intake.doc_request") == []
    assert all(
        item["state"] == "pending" and item["next_reminder_at"] is None
        for item in P15._chase_items(env.app, claim_id).values()
    )

    env.clock.advance_to(P15.T0)  # Monday, 12:00 EAT
    P15._tick(env)
    assert len(P15._drafts(env.app, claim_id, "intake.doc_request")) == 1
    assert all(
        item["state"] == "requested" and item["next_reminder_at"] is not None
        for item in P15._chase_items(env.app, claim_id).values()
    )


def test_ambiguous_held_photos_stay_received_and_are_not_requested(tmp_path):
    model = P15._intimation_model(
        {
            "document_classify": [
                {"doc_type": "photo_damage", "confidence": 0.99},
                {"doc_type": "photo_damage", "confidence": 0.99},
            ],
            "extract": [{"fields": []}, {"fields": []}],
        }
    )
    env = P15._build(tmp_path, "held-photo-ambiguity", model=model)
    P15._set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    P15._emit_email(
        env,
        conversation_id="conv-intake-1",
        body=P15.BODY_CLEAN,
        subject="New motor claim with photos",
        attachments=(
            ("front.pdf", "application/pdf", P15._pdf(["front damage"])),
            ("rear.pdf", "application/pdf", P15._pdf(["rear damage"])),
        ),
    )
    P15._advance(env)
    claim_id = P15._rows(
        env.app, "SELECT id FROM claims ORDER BY created_at, id"
    )[0]["id"]
    P15._approve_ack(env, claim_id)

    photo = P15._chase_items(env.app, claim_id)["photos"]
    assert photo["state"] == "received"
    assert photo["document_id"] is None
    request = P15._drafts(env.app, claim_id, "intake.doc_request")[0]
    payload = request["payload"]["action"]["payload"]
    assert "photos" in payload["received"]
    assert "photos" not in {row["item_id"] for row in payload["outstanding"]}


def test_inbound_reply_defers_every_outstanding_item(tmp_path):
    env = P15._build(tmp_path, "whole-checklist-deferral", model=P15._intimation_model())
    claim_id = P15._to_checklist(env)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chase_items SET next_reminder_at = :later "
                "WHERE item_id = 'photos'"
            ),
            {"later": P15.T0 + timedelta(days=8)},
        )

    P15._at(env, days=7, hours=-1)
    P15._reply(env, body="We are gathering every outstanding document")
    defer_until = env.clock.now + timedelta(hours=48)
    P15._at(env, days=7, hours=1)
    result = P15._tick(env)

    assert result["sent"] == 0
    items = P15._chase_items(env.app, claim_id)
    assert all(
        _aware(item["next_reminder_at"]) >= defer_until
        for item in items.values()
        if item["state"] in {"pending", "requested", "rejected"}
    )


def test_each_rejected_replacement_gets_its_own_rerequest(tmp_path):
    model = P15._intimation_model(
        {
            "document_classify": [
                {"doc_type": "logbook", "confidence": 0.99},
                {"doc_type": "logbook", "confidence": 0.99},
            ],
            "extract": [{"fields": []}, {"fields": []}],
        }
    )
    env = P15._build(tmp_path, "repeat-rerequest", model=model)
    claim_id = P15._to_checklist(env)

    for index in (1, 2):
        P15._reply(
            env,
            body=f"Replacement logbook {index}",
            attachments=(
                (
                    f"logbook-{index}.pdf",
                    "application/pdf",
                    P15._pdf([f"unreadable replacement {index}"]),
                ),
            ),
        )

    drafts = P15._drafts(env.app, claim_id, "chase.rerequest")
    assert len(drafts) == 2
    source_ids = {
        item["payload"]["action"]["payload"]["source_document_id"]
        for item in drafts
    }
    assert len(source_ids) == 2
