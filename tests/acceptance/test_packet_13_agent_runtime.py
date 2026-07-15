"""PACKET-13 acceptance — AR-1 agent runs, AR-2 execute_or_stage gate,
AR-3 outbound service, PRD-05 §5.2 email router + §5.8 assigner.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-13_agent_runtime.md §3.1. No broker, browser, live Graph
mailbox, network call or live model call is permitted: `email.received`
events are synthetic (register #120 — no producer exists before open item 1)
and the classifier is the injected seam.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER_A = "user:01HINTAKEOFFICERA00000AAAA"
OFFICER_B = "user:01HINTAKEOFFICERB00000AAAA"
SELF_ADDRESS = "claims@mayfair.co.ke"

INTAKE_STEP_IDS = [
    "create_claim", "ingest", "populate", "dupe_check",
    "late_check", "acknowledge", "checklist", "triage",
]

AR1_COLUMNS = {
    "id", "agent", "capability_id", "claim_id", "trigger_event", "status",
    "steps", "autonomy_level", "error", "started_at", "ended_at",
}


class FakeClassifier:
    """Injected §5.2 classifier seam; returns a queued script of results."""

    def __init__(self) -> None:
        self.script: list[dict] = []
        self.calls: list[dict] = []

    def classify(self, message: dict) -> dict:
        self.calls.append(message)
        if not self.script:
            raise AssertionError("classifier called without a scripted result")
        return self.script.pop(0)


def _h(actor: str) -> dict[str, str]:
    return {"X-Actor": actor}


def _drain(app, cycles: int = 32) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _emit(app, event_type: str, payload: dict, claim_id: str | None = None) -> str:
    with Session(app.state.engine) as session:
        event = app.state.record_event(
            session,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            actor=AGENT,
            correlation_id=None,
        )
        session.commit()
        return event.id


def _decode(row: dict) -> dict:
    if isinstance(row.get("payload"), str):
        row["payload"] = json.loads(row["payload"])
    return row


def _rows(app, sql: str, **params) -> list[dict]:
    with app.state.engine.connect() as connection:
        return [
            _decode(dict(row))
            for row in connection.execute(text(sql), params).mappings()
        ]


def _events(app, event_type: str) -> list[dict]:
    return _rows(
        app,
        "SELECT id, claim_id, payload FROM events WHERE type = :t ORDER BY seq",
        t=event_type,
    )


def _items(app, **filters) -> list[dict]:
    clauses = " AND ".join(f"{key} = :{key}" for key in filters) or "1=1"
    return _rows(
        app,
        "SELECT id, claim_id, type, subtype, status, payload FROM review_items "
        f"WHERE {clauses} ORDER BY created_at, id",
        **filters,
    )


def _ledger_actions(app) -> list[str]:
    return [
        row["action"]
        for row in _rows(app, "SELECT action FROM audit_ledger ORDER BY seq")
    ]


def _set_level(app, capability_id: str, level: str) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
            {"level": level, "id": capability_id},
        )


def _capability(app, capability_id: str) -> dict:
    rows = _rows(
        app,
        "SELECT id, current_level, max_level FROM capabilities WHERE id = :id",
        id=capability_id,
    )
    assert rows, f"capability {capability_id} is not registered"
    return rows[0]


def _build(tmp_path, name: str, *, clock=None):
    from fastapi.testclient import TestClient

    from agent_runtime import build_agent_runtime
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from intake_agent import build_intake_agent
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(url, clock=clock) if clock is not None else create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles={OFFICER_A: "claims_officer", OFFICER_B: "claims_officer"})
    runtime = build_agent_runtime(app)
    classifier = FakeClassifier()
    build_intake_agent(
        app,
        classifier=classifier,
        officers=[OFFICER_A, OFFICER_B],
        config={"self_addresses": [SELF_ADDRESS], "archive_sample_rate": 0},
    )
    return TestClient(app), app, runtime, classifier


@pytest.fixture()
def env(tmp_path):
    client, app, runtime, classifier = _build(tmp_path, "pacha_acc13")
    return {"client": client, "app": app, "runtime": runtime, "classifier": classifier}


def _claim(client) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.0.0"},
        headers=_h(AGENT),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_party(app, claim_id: str, *, role: str = "insured") -> str:
    from claim_core import new_ulid

    party_id = new_ulid()
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO parties (id, claim_id, role, name, email)"
                " VALUES (:id, :claim_id, :role, 'Amina Wanjiku',"
                " 'amina@example.co.ke')"
            ),
            {"id": party_id, "claim_id": claim_id, "role": role},
        )
    return party_id


def _seed_thread(app, claim_id: str, thread_id: str) -> None:
    from claim_core import new_ulid

    app.state.blob_store.put(f"seed/{thread_id}", b"seed body")
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO communications (id, claim_id, direction, channel,"
                " graph_message_id, thread_id, from_addr, subject, body_s3_key,"
                " occurred_at)"
                " VALUES (:id, :claim_id, 'inbound', 'email', :message_id,"
                " :thread_id, 'amina@example.co.ke', 'Claim intimation',"
                " :body_key, :occurred_at)"
            ),
            {
                "id": new_ulid(),
                "claim_id": claim_id,
                "message_id": f"seed-{thread_id}",
                "thread_id": thread_id,
                "body_key": f"seed/{thread_id}",
                "occurred_at": datetime.now(UTC),
            },
        )


def _mail(
    message_id: str,
    *,
    conversation_id: str | None = None,
    from_addr: str = "amina@example.co.ke",
    subject: str = "Re: claim",
    body_text: str = "see attached",
    attachments: list[dict] | None = None,
) -> dict:
    return {
        "graph_message_id": message_id,
        "conversation_id": conversation_id,
        "from_addr": from_addr,
        "to_addrs": [SELF_ADDRESS],
        "subject": subject,
        "body_text": body_text,
        "attachments": attachments
        if attachments is not None
        else [
            {
                "filename": "photo.png",
                "mime": "image/png",
                "content_b64": base64.b64encode(
                    f"fake-image-bytes-{message_id}".encode()
                ).decode("ascii"),
            }
        ],
    }


def _claim_count(app) -> int:
    return _rows(app, "SELECT COUNT(*) AS n FROM claims")[0]["n"]


# --- AR-1 -----------------------------------------------------------------------------


def test_agent_runs_table_matches_ar1_ddl(env):
    app = env["app"]
    from sqlalchemy import inspect

    columns = {column["name"] for column in inspect(app.state.engine).get_columns("agent_runs")}
    assert columns == AR1_COLUMNS


def test_cop_steps_pack_data_declares_intake_flow():
    payload = yaml.safe_load((MOTOR_PACK / "cop_steps.yaml").read_text(encoding="utf-8"))
    definitions = {row["capability_id"]: row for row in payload["step_definitions"]}
    steps = [step["id"] for step in definitions["intake.claim_creation"]["steps"]]
    assert steps == INTAKE_STEP_IDS


# --- AR-2 gate ------------------------------------------------------------------------


def test_gate_l0_logs_l1_drafts_l2_confirms(env):
    from agent_runtime import Action

    app, runtime, client = env["app"], env["runtime"], env["client"]
    claim_id = _claim(client)
    _drain(app)
    calls: list[dict] = []
    runtime.register_executor("test.echo", lambda action: calls.append(action.payload))

    _set_level(app, "icon.claim_register", "L0")
    logged = runtime.execute_or_stage(
        capability_id="icon.claim_register",
        action=Action(type="test.echo", payload={"n": 0}),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert logged["status"] == "logged"
    assert calls == []

    drafted = runtime.execute_or_stage(
        capability_id="intake.acknowledge",  # launch level L1 (§5.6)
        action=Action(type="test.echo", payload={"n": 1}),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert drafted["status"] == "staged"
    assert drafted["review_type"] == "DRAFT_RELEASE"
    assert calls == []
    _drain(app)
    assert _items(app, type="DRAFT_RELEASE", claim_id=claim_id)

    _set_level(app, "assessment.mode_confirm", "L2")
    confirmed = runtime.execute_or_stage(
        capability_id="assessment.mode_confirm",
        action=Action(type="test.echo", payload={"n": 2}),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert confirmed["status"] == "staged"
    assert confirmed["review_type"] == "MODE_CONFIRM"  # gate.yaml confirm_types
    assert calls == []

    _set_level(app, "intake.claim_creation", "L2")
    unmapped = runtime.execute_or_stage(
        capability_id="intake.claim_creation",
        action=Action(type="test.echo", payload={"n": 3}),
        claim_id=claim_id,
        actor=AGENT,
    )
    # register #126: unmapped L2 confirm type fails closed to the L1 draft path
    assert unmapped["status"] == "staged"
    assert unmapped["review_type"] == "DRAFT_RELEASE"
    assert calls == []


def test_gate_l3_samples_and_l4_executes(env):
    import json

    from agent_runtime import Action

    app, runtime, client = env["app"], env["runtime"], env["client"]
    claim_id = _claim(client)
    _drain(app)
    calls: list[dict] = []
    runtime.register_executor("test.echo", lambda action: calls.append(action.payload))

    _set_level(app, "intake.acknowledge", "L4")
    executed = runtime.execute_or_stage(
        capability_id="intake.acknowledge",
        action=Action(type="test.echo", payload={"n": 4}),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert executed["status"] == "executed"
    assert calls == [{"n": 4}]
    assert executed["sampled"] is False

    with app.state.engine.begin() as connection:
        raw = connection.execute(
            text("SELECT policy FROM capabilities WHERE id = 'intake.acknowledge'")
        ).scalar()
        policy = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        policy["sampling_rate"] = 100
        connection.execute(
            text("UPDATE capabilities SET policy = :policy, current_level = 'L3'"
                 " WHERE id = 'intake.acknowledge'"),
            {"policy": json.dumps(policy)},
        )
    sampled = runtime.execute_or_stage(
        capability_id="intake.acknowledge",
        action=Action(type="test.echo", payload={"n": 5}),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert sampled["status"] == "executed"
    assert calls[-1] == {"n": 5}
    assert sampled["sampled"] is True
    _drain(app)
    assert _items(app, type="SAMPLE_REVIEW", claim_id=claim_id)


def test_blocked_grade_forces_draft_at_l4(tmp_path):
    from fastapi.testclient import TestClient

    from agent_runtime import Action, build_agent_runtime
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc13_gate.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles={OFFICER_A: "claims_officer"})
    runtime = build_agent_runtime(
        app, grade=lambda action, capability_id, claim_id: SimpleNamespace(blocked=True)
    )
    client = TestClient(app)
    claim_id = _claim(client)
    calls: list[dict] = []
    runtime.register_executor("test.echo", lambda action: calls.append(action.payload))
    _set_level(app, "intake.acknowledge", "L4")

    outcome = runtime.execute_or_stage(
        capability_id="intake.acknowledge",
        action=Action(type="test.echo", payload={"n": 6}, grader_id="G-COMM"),
        claim_id=claim_id,
        actor=AGENT,
    )
    assert outcome["status"] == "staged"
    assert outcome["review_type"] == "DRAFT_RELEASE"
    assert calls == []


def test_no_funds_transfer_action_is_registrable(env):
    runtime = env["runtime"]
    for forbidden in ("settlement.eft_transfer", "settlement.pay", "icon.payment_voucher.execute"):
        with pytest.raises(ValueError):
            runtime.register_executor(forbidden, lambda action: None)


# --- AR-3 comms -----------------------------------------------------------------------


def test_comms_send_gcomm_template_and_draft_paths(env):
    app, runtime, client = env["app"], env["runtime"], env["client"]
    claim_id = _claim(client)
    _drain(app)
    party_id = _seed_party(app, claim_id)

    unknown = runtime.comms.send(
        template_id="T-99",
        claim_id=claim_id,
        to_party_ids=[party_id],
        attachments=(),
        capability_id="intake.acknowledge",
        actor=AGENT,
    )
    assert unknown["status"] == "refused"
    assert unknown["code"] == "TEMPLATE_NOT_REGISTERED"

    stranger = runtime.comms.send(
        template_id="T-06a",
        claim_id=claim_id,
        to_party_ids=["01HNOTAPARTYOFTHISCLAIMAAA"],
        attachments=(),
        capability_id="intake.acknowledge",
        actor=AGENT,
    )
    assert stranger["status"] == "refused"
    assert stranger["code"] == "G_COMM_FAILED"
    _drain(app)
    assert _items(app, type="EXCEPTION", claim_id=claim_id)

    staged = runtime.comms.send(
        template_id="T-06a",
        claim_id=claim_id,
        to_party_ids=[party_id],
        attachments=(),
        capability_id="intake.acknowledge",
        actor=AGENT,
    )
    # launch level L1: every send stages as a draft; T-06a body is
    # pending_capture (item 6) and the draft must say so visibly.
    assert staged["status"] == "staged"
    _drain(app)
    drafts = _items(app, type="DRAFT_RELEASE", claim_id=claim_id)
    assert drafts
    assert "pending_capture" in str(drafts[-1]["payload"])
    # no real send happened: no outbound communications row, no email.sent
    outbound = _rows(
        app,
        "SELECT COUNT(*) AS n FROM communications WHERE direction = 'outbound'",
    )[0]["n"]
    assert outbound == 0
    assert _events(app, "email.sent") == []


def test_send_window_decision_sunday(tmp_path):
    # Sunday 2026-07-19 20:00 EAT == 17:00 UTC — outside AR-3a for
    # non-exempt sends; intake.acknowledge is exempt (24/7).
    clock = lambda: datetime(2026, 7, 19, 17, 0, tzinfo=UTC)  # noqa: E731
    client, app, runtime, _classifier = _build(tmp_path, "pacha_acc13_window", clock=clock)
    claim_id = _claim(client)
    _drain(app)
    party_id = _seed_party(app, claim_id)

    queued = runtime.comms.send(
        template_id="T-06",
        claim_id=claim_id,
        to_party_ids=[party_id],
        attachments=(),
        capability_id="intake.doc_request",
        actor=AGENT,
    )
    assert queued["status"] == "queued_window"

    exempt = runtime.comms.send(
        template_id="T-06a",
        claim_id=claim_id,
        to_party_ids=[party_id],
        attachments=(),
        capability_id="intake.acknowledge",
        actor=AGENT,
    )
    assert exempt["status"] == "staged"


# --- §5.2 email router ----------------------------------------------------------------


def test_scenario_2_thread_replay_zero_new_claims(env):
    app, client = env["app"], env["client"]
    claim_id = _claim(client)
    _drain(app)
    _seed_thread(app, claim_id, "thread-alpha")
    documents_before = _rows(
        app, "SELECT COUNT(*) AS n FROM documents WHERE claim_id = :c", c=claim_id
    )[0]["n"]
    claims_before = _claim_count(app)

    for index in range(50):
        _emit(
            app,
            "email.received",
            _mail(f"replay-{index}", conversation_id="thread-alpha"),
        )
    _drain(app, cycles=128)

    assert _claim_count(app) == claims_before, "zero new claims across 50-email replay"
    documents_after = _rows(
        app, "SELECT COUNT(*) AS n FROM documents WHERE claim_id = :c", c=claim_id
    )[0]["n"]
    assert documents_after == documents_before + 50
    assert len(_events(app, "document.received")) >= 50

    # redelivery of an already-processed graph_message_id is a no-op
    _emit(app, "email.received", _mail("replay-0", conversation_id="thread-alpha"))
    _drain(app)
    assert _rows(
        app, "SELECT COUNT(*) AS n FROM documents WHERE claim_id = :c", c=claim_id
    )[0]["n"] == documents_after


def test_reference_match_and_ambiguous(env):
    from claim_core import FieldWrite

    app, client = env["app"], env["client"]
    claim_1 = _claim(client)
    claim_2 = _claim(client)
    _drain(app)
    for claim_id in (claim_1, claim_2):
        app.state.claim_service.write_fields(
            claim_id,
            [
                FieldWrite(
                    path="vehicle.reg",
                    value="KDA 123A",
                    value_type="string",
                    source_type="human",
                    source_ref={"user_id": OFFICER_A, "review_item_id": "fixture"},
                    verification_state="human_verified",
                )
            ],
            OFFICER_A,
        )

    # our claim id in the subject matches exactly one open claim
    _emit(app, "email.received", _mail("ref-1", subject=f"claim {claim_1} documents"))
    _drain(app)
    attached = _events(app, "INBOUND_ATTACHED")  # §5.2 names the event verbatim
    assert attached and attached[-1]["claim_id"] == claim_1

    # reg plate matching two open claims must never guess
    _emit(app, "email.received", _mail("ref-2", subject="loss for KDA 123A"))
    _drain(app)
    ambiguous = _items(app, subtype="ambiguous_inbound")
    assert len(ambiguous) == 1
    payload = ambiguous[0]["payload"]
    assert claim_1 in str(payload) and claim_2 in str(payload)
    assert _claim_count(app) == 2


def test_classifier_contract_boundaries_verbatim(env):
    app, classifier = env["app"], env["classifier"]
    cases = [
        ("cls-1", {"class": "new_intimation", "confidence": 0.85}, "intake_requested"),
        ("cls-2", {"class": "new_intimation", "confidence": 0.8499}, "doc_classify"),
        ("cls-3", {"class": "multi_intimation", "confidence": 0.99}, "multi"),
        ("cls-4", {"class": "claim_related_unmatched", "confidence": 0.99}, "doc_classify"),
        ("cls-5", {"class": "not_a_claim", "confidence": 0.95}, "archived"),
        ("cls-6", {"class": "not_a_claim", "confidence": 0.9499}, "doc_classify"),
        ("cls-7", {"class": "unclear", "confidence": 0.99}, "doc_classify"),
    ]
    for message_id, result, _expected in cases:
        classifier.script.append(result)
        _emit(app, "email.received", _mail(message_id, subject=f"unmatched {message_id}"))
        _drain(app)

    requested = _events(app, "intake.requested")
    assert len(requested) == 1
    assert requested[0]["payload"]["graph_message_id"] == "cls-1"
    assert _claim_count(app) == 0, "classification never creates a claim in this slice"

    doc_classify = _items(app, type="DOC_CLASSIFY", subtype="mailbox_triage")
    classified_ids = {item["payload"].get("graph_message_id") for item in doc_classify}
    assert {"cls-2", "cls-4", "cls-6", "cls-7"} <= classified_ids

    multi = _items(app, type="EXCEPTION", subtype="multi_claim_email")
    assert len(multi) == 1

    archived = _events(app, "mail.archived")
    assert len(archived) == 1
    assert archived[0]["payload"]["graph_message_id"] == "cls-5"
    # archive_sample_rate is 0 in this fixture: no SAMPLE_REVIEW
    assert not _items(app, type="SAMPLE_REVIEW")


def test_scenario_5_self_sent_mail_ignored(env):
    app = env["app"]
    communications_before = _rows(app, "SELECT COUNT(*) AS n FROM communications")[0]["n"]
    _emit(app, "email.received", _mail("loop-1", from_addr=SELF_ADDRESS))
    _drain(app)
    assert _rows(app, "SELECT COUNT(*) AS n FROM communications")[0]["n"] == (
        communications_before
    )
    assert _claim_count(app) == 0
    assert not _items(app)
    assert _events(app, "intake.requested") == []


def test_terminal_state_inbound_attach_never_transition(env):
    app, client = env["app"], env["client"]
    declined = _claim(client)
    settled = _claim(client)
    _drain(app)
    _seed_thread(app, declined, "thread-declined")
    _seed_thread(app, settled, "thread-settled")
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET status = 'DECLINED' WHERE id = :c"), {"c": declined}
        )
        connection.execute(
            text("UPDATE claims SET status = 'SETTLED' WHERE id = :c"), {"c": settled}
        )

    _emit(app, "email.received", _mail("term-1", conversation_id="thread-declined"))
    _emit(app, "email.received", _mail("term-2", conversation_id="thread-settled"))
    _drain(app)

    states = {
        row["id"]: row["status"]
        for row in _rows(app, "SELECT id, status FROM claims")
    }
    assert states[declined] == "DECLINED"
    assert states[settled] == "SETTLED"

    reopen = _items(app, type="REOPEN_PROMPT")
    assert len(reopen) == 1
    assert reopen[0]["claim_id"] == declined
    assert not [item for item in _items(app) if item["claim_id"] == settled]

    for claim_id in (declined, settled):
        documents = _rows(
            app, "SELECT COUNT(*) AS n FROM documents WHERE claim_id = :c", c=claim_id
        )[0]["n"]
        assert documents == 1, "terminal inbound must still attach"


# --- §5.8 assigner ---------------------------------------------------------------------


def test_assigner_weighted_round_robin_and_ledger(env):
    app, client = env["app"], env["client"]
    seed_1 = _claim(client)
    seed_2 = _claim(client)
    _drain(app)
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :a WHERE id IN (:c1, :c2)"),
            {"a": OFFICER_A, "c1": seed_1, "c2": seed_2},
        )

    newcomer = _claim(client)
    _drain(app)
    assigned = _rows(
        app, "SELECT assigned_to FROM claims WHERE id = :c", c=newcomer
    )[0]["assigned_to"]
    assert assigned == OFFICER_B, "weighted round-robin picks the least-loaded officer"
    assert _events(app, "claim.assigned")
    assert "claim.assigned" in _ledger_actions(app)


# --- §5.6 capability reconciliation ---------------------------------------------------


def test_capabilities_match_prd05_table(env):
    app = env["app"]
    doc_request = _capability(app, "intake.doc_request")
    assert doc_request["max_level"] == "L4"
    assert doc_request["current_level"] == "L1"
    assert _capability(app, "intake.claim_creation")["current_level"] == "L2"
    assert _capability(app, "intake.acknowledge")["current_level"] == "L1"
    assert _capability(app, "triage.decline_draft")["max_level"] == "L2"
    assert _capability(app, "triage.coverage_check")["max_level"] == "L3"
    assert _capability(app, "triage.ex_gratia")["max_level"] == "L1"

    from eval_harness import PromotionDenied

    with pytest.raises(PromotionDenied):
        app.state.eval_harness.autonomy.request_promotion(
            "triage.ex_gratia",
            "L2",
            sign_offs=[{"actor": OFFICER_A, "role": "claims_manager"}],
            actor=OFFICER_A,
        )


def test_template_registry_matches_prd05_55():
    payload = yaml.safe_load(
        (MOTOR_PACK / "templates" / "registry.yaml").read_text(encoding="utf-8")
    )
    templates = {row["id"]: row for row in payload["templates"]}
    assert "T-06a" in templates, "§5.5 acknowledgement template must be registered"
    for template_id in ("T-06a", "T-06"):
        assert templates[template_id]["min_verification"] == "extracted"
        assert templates[template_id]["status"] == "pending_capture"
