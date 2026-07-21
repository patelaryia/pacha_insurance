"""Regression probes for PACKET-16 reviewer findings."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text


def _acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance/test_packet_16_assessment_dispatch.py"
    spec = importlib.util.spec_from_file_location("packet16_acceptance_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P16 = _acceptance_module()


def _new_intimation_with_estimate(tmp_path):
    env = P16._build(tmp_path, "held-estimate", model=P16._model())
    P16._set_level(env.app, "intake.claim_creation", "L3")
    env.classifier.script.append({"class": "new_intimation", "confidence": 0.92})
    P16._emit_email(
        env,
        conversation_id="conv-held-estimate",
        body=P16.BODY_CLEAN,
        subject="New motor claim with estimate",
        attachments=(
            (
                "estimate.pdf",
                "application/pdf",
                P16._pdf(["Repair estimate. Grand Total: KES 90,000"]),
            ),
            ("damage.png", "image/png", P16._png()),
        ),
    )
    P16._advance(env)
    claim_id = P16._rows(
        env.app, "SELECT id FROM claims ORDER BY created_at, id"
    )[0]["id"]
    return env, claim_id


def test_intimation_estimate_replays_when_claim_reaches_triaged(tmp_path):
    env, claim_id = _new_intimation_with_estimate(tmp_path)

    assert P16._field(env.client, claim_id, "assessment.estimate_total")["value"] == 9_000_000
    assert P16._status(env, claim_id) == "INTIMATED"
    assert not [
        item
        for item in P16._items(env.app, claim_id=claim_id, type="EXCEPTION")
        if item["subtype"] == "assessment_out_of_sequence"
    ]
    assert P16._open_items(env.app, claim_id, "MODE_CONFIRM") == []

    P16._approve_ack(env, claim_id)
    P16._resolve_coverage(env, claim_id)

    assert P16._status(env, claim_id) == "IN_ASSESSMENT"
    assert len(P16._open_items(env.app, claim_id, "MODE_CONFIRM")) == 1


def test_shadow_result_is_durable_redacted_and_failure_does_not_drop_reissue(tmp_path):
    env = P16._build(tmp_path, "shadow-outcomes", model=P16._model())
    claim_id, card = P16._to_mode_card(env)

    first = P16._rows(
        env.app,
        "SELECT autonomy_level, steps FROM agent_runs "
        "WHERE capability_id = 'assessment.mode_shadow' ORDER BY started_at, id",
    )
    assert len(first) == 1
    assert first[0]["autonomy_level"] == "L0"
    logged = first[0]["steps"][-1]["outcome"]["log_payload"]
    assert logged == {
        "status": "completed",
        "mode_card_id": card["payload"]["review_id"],
        "mode": "physical",
        "confidence": 0.83,
        "rationale": "__redacted__",
    }
    assert P16.SHADOW_RATIONALE not in str(first)

    response = P16._resolve(
        env,
        card["id"],
        P16.OFFICER_A,
        action="reject",
        schema_version="MODE_CONFIRM@2",
        payload={
            "capability_id": "assessment.mode_confirm",
            "diff": P16._diff(),
            "reason": "photos unclear",
            "decision": {"mode": "physical", "vendor_ids": ["V-ALPHA"]},
        },
    )
    assert response.status_code == 200, response.text
    P16._drain(env.app)

    reissued = P16._open_items(env.app, claim_id, "MODE_CONFIRM")
    assert len(reissued) == 1
    assert reissued[0]["id"] != card["id"]
    runs = P16._rows(
        env.app,
        "SELECT steps FROM agent_runs WHERE capability_id = 'assessment.mode_shadow' "
        "ORDER BY started_at, id",
    )
    assert len(runs) == 2
    failed = runs[1]["steps"][-1]["outcome"]["log_payload"]
    assert failed == {
        "status": "failed",
        "mode_card_id": reissued[0]["payload"]["review_id"],
        "error_type": "AssertionError",
        "rationale": "__redacted__",
    }


def test_missing_broker_blocks_before_mode_decision_commits(tmp_path):
    env = P16._build(tmp_path, "missing-broker", model=P16._model())
    claim_id, card = P16._to_mode_card(env)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE parties SET meta = :meta "
                "WHERE claim_id = :claim_id AND role = 'broker'"
            ),
            {"meta": "{}", "claim_id": claim_id},
        )

    response = P16._approve_mode(
        env,
        card,
        mode="desk",
        vendor_ids=["V-ALPHA"],
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "RESOLUTION_BLOCKED_ON_INPUTS"
    assert P16._field(env.client, claim_id, "assessment.mode") is None
    assert P16._events(env.app, "assessment.mode_decided", claim_id) == []
    assert P16._open_items(env.app, claim_id, "MODE_CONFIRM")[0]["id"] == card["id"]
