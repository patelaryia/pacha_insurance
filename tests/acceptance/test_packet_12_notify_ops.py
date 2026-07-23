"""PACKET-12 acceptance — AR-5 notify module, digest, SLA board, portfolio,
admin surfaces, scenario-2 decline visibility.

Protected (CODEOWNERS): the builder may not modify this file. Contract per
docs/packets/PACKET-12_ops_surfaces.md §3.1. No broker, browser, live Graph
send, network call or production secret is permitted; the email channel must
stage visibly (open item 1) and the websocket authenticates with the injected
fake verifier.
"""
from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from ulid import ULID

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:intake"
OFFICER = "user:01HOPSOFFICER000000000AAAA"
OFFICER_2 = "user:01HOPSOFFICER000000000BBBB"
ACM = "user:01HOPSASSTMANAGER00000AAAA"
CM = "user:01HOPSCLAIMSMANAGER000AAAA"
ADMIN = "user:01HOPSADMIN00000000000AAAA"
AUDITOR = "user:01HOPSAUDITOR000000000AAAA"

TENANT = "11111111-1111-1111-1111-111111111111"
OIDS = {
    "officer-token": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "officer-2-token": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "acm-token": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "cm-token": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "admin-token": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    "auditor-token": "ffffffff-ffff-ffff-ffff-ffffffffffff",
}
ACTORS = {
    "officer-token": OFFICER,
    "officer-2-token": OFFICER_2,
    "acm-token": ACM,
    "cm-token": CM,
    "admin-token": ADMIN,
    "auditor-token": AUDITOR,
}
ROLES = {
    OFFICER: "claims_officer",
    OFFICER_2: "claims_officer",
    ACM: "asst_claims_manager",
    CM: "claims_manager",
    ADMIN: "admin",
    AUDITOR: "auditor",
}
IDENTITIES = {f"{TENANT}:{OIDS[token]}": ACTORS[token] for token in OIDS}


class FakeVerifier:
    """Pinned TokenVerifier seam (PACKET-11); unknown tokens are invalid."""

    def verify(self, token: str):
        from review_queue.auth import TokenClaims, TokenVerificationError

        oid = OIDS.get(token)
        if oid is None:
            raise TokenVerificationError("unknown fixture token")
        return TokenClaims(tid=TENANT, oid=oid)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def _rows(app, sql: str, **params) -> list[dict]:
    with app.state.engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(sql), params).mappings()]


def _notifications(app, **filters) -> list[dict]:
    clauses = " AND ".join(f"{key} = :{key}" for key in filters) or "1=1"
    return _rows(
        app,
        "SELECT id, recipient, rule_id, event_id, claim_id, channel, status "
        f"FROM notifications WHERE {clauses} ORDER BY created_at, id",
        **filters,
    )


def _ledger_actions(app) -> list[str]:
    return [
        row["action"]
        for row in _rows(app, "SELECT action FROM audit_ledger ORDER BY seq")
    ]


@pytest.fixture()
def env(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core import create_app
    from claim_core.schemas import ClaimCreate
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from notify import build_notify
    from review_queue import build_review_queue, install_console, install_ops

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/pacha_acc12.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles=dict(ROLES))
    handle = build_notify(app, roles=dict(ROLES))
    install_ops(app)

    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), AGENT
    )
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :actor WHERE id = :claim_id"),
            {"actor": OFFICER, "claim_id": claim.id},
        )
    _drain(app)

    install_console(
        app, verifier=FakeVerifier(), identities=dict(IDENTITIES), roles=dict(ROLES)
    )
    client = TestClient(app)
    return {"client": client, "app": app, "claim_id": claim.id, "notify": handle}


def _seed_clock(
    app,
    claim_id: str,
    definition_id: str,
    *,
    breach_delta_hours: int,
    state: str = "running",
) -> str:
    clock_id = str(ULID())
    now = datetime.now(UTC)
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sla_clocks (id, claim_id, definition_id, started_at,"
                " stopped_at, warn_at, breach_at, state, started_by_event,"
                " stopped_by_event) VALUES (:id, :claim_id, :definition_id,"
                " :started_at, NULL, NULL, :breach_at, :state, 'fixture', NULL)"
            ),
            {
                "id": clock_id,
                "claim_id": claim_id,
                "definition_id": definition_id,
                "started_at": now - timedelta(hours=48),
                "breach_at": now + timedelta(hours=breach_delta_hours),
                "state": state,
            },
        )
    return clock_id


def _seed_definition(app, definition_id: str, escalate_to_role: str) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sla_definitions (id, name, start_event, stop_event,"
                " warn_after, breach_after, escalate_to_role, calendar, status)"
                " VALUES (:id, :id, 'fixture.start', NULL, NULL, '24h',"
                " :role, 'business', 'active')"
            ),
            {"id": definition_id, "role": escalate_to_role},
        )


# --- notify rules + websocket -------------------------------------------------------


def test_sla_breached_notifies_assigned_officer_and_pushes_websocket(env):
    client, app, claim_id = env["client"], env["app"], env["claim_id"]

    with client.websocket_connect("/console/ops/ws") as ws:
        ws.send_json({"token": "officer-token"})
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["actor"] == OFFICER

        event_id = _emit(
            app,
            "sla.breached",
            {"definition_id": "sla.acknowledge", "clock_id": "fixture-clock"},
            claim_id,
        )
        _drain(app)

        pushed = ws.receive_json()
        assert pushed["type"] == "notification"
        assert pushed["event_type"] == "sla.breached"
        assert pushed["claim_id"] == claim_id

    in_app = _notifications(
        app, event_id=event_id, recipient=OFFICER, channel="in_app"
    )
    assert len(in_app) == 1
    assert in_app[0]["status"] == "sent"

    # idempotent projection
    _drain(app)
    assert len(
        _notifications(app, event_id=event_id, recipient=OFFICER, channel="in_app")
    ) == 1

    actions = _ledger_actions(app)
    assert "notify.sent" in actions


def test_email_channel_stages_visibly_blocked_on_item_1(env):
    client, app, claim_id = env["client"], env["app"], env["claim_id"]
    event_id = _emit(
        app,
        "sla.breached",
        {"definition_id": "sla.acknowledge", "clock_id": "fixture-clock-2"},
        claim_id,
    )
    _drain(app)

    email = _notifications(app, event_id=event_id, channel="email")
    assert email, "email channel row missing — must stage, never drop"
    assert all(row["status"] == "staged" for row in email)
    assert "notify.staged" in _ledger_actions(app)

    listing = client.get(
        "/console/ops/notifications?scope=mine", headers=_bearer("officer-token")
    )
    assert listing.status_code == 200, listing.text
    staged = [
        item
        for item in listing.json()["items"]
        if item["channel"] == "email" and item["event_id"] == event_id
    ]
    assert staged
    assert "blocked_on" in str(staged[0]["payload"])


def test_grader_failed_notifies_cm_on_critical_only(env):
    app = env["app"]
    critical_id = _emit(
        app,
        "grader.failed",
        {"grader_id": "G-MONEY", "severity": "critical", "capability_id": "x"},
    )
    non_critical_id = _emit(
        app,
        "grader.failed",
        {"grader_id": "G-NOTE", "severity": "advisory", "capability_id": "x"},
    )
    demoted_id = _emit(
        app, "autonomy.demoted", {"capability_id": "triage.route", "to_level": 0}
    )
    _drain(app)

    assert _notifications(app, event_id=critical_id, recipient=CM)
    assert not _notifications(app, event_id=non_critical_id)
    assert _notifications(app, event_id=demoted_id, recipient=CM)


def test_websocket_rejects_bad_token_with_4401(env):
    from starlette.websockets import WebSocketDisconnect

    client = env["client"]
    with client.websocket_connect("/console/ops/ws") as ws:
        ws.send_json({"token": "not-a-real-token"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4401


def test_notification_read_marking_own_rows_only(env):
    client, app, claim_id = env["client"], env["app"], env["claim_id"]
    event_id = _emit(
        app,
        "sla.breached",
        {"definition_id": "sla.acknowledge", "clock_id": "fixture-clock-3"},
        claim_id,
    )
    _drain(app)
    row = _notifications(app, event_id=event_id, recipient=OFFICER, channel="in_app")[0]

    other = client.post(
        f"/console/ops/notifications/{row['id']}/read",
        headers=_bearer("officer-2-token"),
    )
    assert other.status_code in (403, 404)

    own = client.post(
        f"/console/ops/notifications/{row['id']}/read",
        headers=_bearer("officer-token"),
    )
    assert own.status_code == 200, own.text
    assert (
        _notifications(app, event_id=event_id, recipient=OFFICER, channel="in_app")[0][
            "status"
        ]
        == "read"
    )


# --- digest -------------------------------------------------------------------------


def test_digest_per_officer_idempotent(env):
    app, handle, claim_id = env["app"], env["notify"], env["claim_id"]
    _emit(
        app,
        "review.created",
        {
            "type": "NOTE_REVIEW",
            "capability_id": "pack.note_draft",
            "output": {"note": "draft"},
            "citations": [{"document_id": "d", "page": 1}],
        },
        claim_id,
    )
    _drain(app)

    now = datetime(2026, 7, 15, 5, 0, tzinfo=UTC)  # 08:00 EAT
    handle.run_digest(now)
    digests = _notifications(app, recipient=OFFICER, rule_id="digest")
    in_app = [row for row in digests if row["channel"] == "in_app"]
    assert len(in_app) == 1
    assert in_app[0]["status"] == "sent"
    emails = [row for row in digests if row["channel"] == "email"]
    assert all(row["status"] == "staged" for row in emails)

    # unassigned officer gets no digest
    assert not _notifications(app, recipient=OFFICER_2, rule_id="digest")

    # idempotent for the same EAT date
    handle.run_digest(now + timedelta(hours=2))
    assert (
        len(
            [
                row
                for row in _notifications(app, recipient=OFFICER, rule_id="digest")
                if row["channel"] == "in_app"
            ]
        )
        == 1
    )


def test_digest_schedule_is_pack_config_eat(env):
    import yaml

    config = yaml.safe_load(
        (MOTOR_PACK / "notify" / "notify.yaml").read_text(encoding="utf-8")
    )
    digest = config["digest"]
    assert digest["timezone"] == "Africa/Nairobi"
    assert int(digest["hour"]) == 8
    assert config["staff_domain_allowlist"], "staff allowlist must be pack data"


# --- SLA board + bulk escalate ------------------------------------------------------


def test_sla_board_sorted_and_bulk_escalate(env):
    client, app, claim_id = env["client"], env["app"], env["claim_id"]
    _seed_definition(app, "sla.fixture_escalatable", "claims_manager")
    near = _seed_clock(
        app, claim_id, "sla.fixture_escalatable", breach_delta_hours=1
    )
    far = _seed_clock(app, claim_id, "sla.acknowledge", breach_delta_hours=72)

    board = client.get("/console/ops/sla-board", headers=_bearer("cm-token"))
    assert board.status_code == 200, board.text
    clocks = board.json()["clocks"]
    ids = [row["clock_id"] for row in clocks]
    assert ids.index(near) < ids.index(far), "sorted by breach proximity"

    denied = client.post(
        "/console/ops/sla-board/escalate",
        json={"clock_ids": [near]},
        headers=_bearer("officer-token"),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_ROLE"

    cm_rows_before = len(_notifications(app, recipient=CM, channel="in_app"))
    result = client.post(
        "/console/ops/sla-board/escalate",
        json={"clock_ids": [near, far]},
        headers=_bearer("cm-token"),
    )
    assert result.status_code == 200, result.text
    outcomes = {row["clock_id"]: row["outcome"] for row in result.json()["results"]}
    assert outcomes[near] == "escalated"
    # shipped definitions carry escalate_to_role: pending_capture — never guessed
    assert outcomes[far] == "blocked_on_inputs"

    _drain(app)
    assert "sla.escalated" in _ledger_actions(app)
    cm_rows_after = len(_notifications(app, recipient=CM, channel="in_app"))
    assert cm_rows_after == cm_rows_before + 1, (
        "escalation must notify the escalate_to_role recipients"
    )


# --- portfolio ----------------------------------------------------------------------


def test_portfolio_tiles_roles_and_csv(env):
    client = env["client"]

    forbidden = client.get(
        "/console/ops/portfolio", headers=_bearer("officer-token")
    )
    assert forbidden.status_code == 403

    response = client.get("/console/ops/portfolio", headers=_bearer("cm-token"))
    assert response.status_code == 200, response.text
    tiles = {tile["series_id"]: tile for tile in response.json()["tiles"]}

    for live_id in (
        "open_claims_by_state",
        "sla_breaches",
        "per_officer_queue_depth",
        "aging_histogram",
        "savings_mtd_ytd",
    ):
        assert tiles[live_id]["status"] == "live", live_id

    open_states = tiles["open_claims_by_state"]["data"]
    assert any(row.get("state") == "INTIMATED" for row in open_states) or any(
        "INTIMATED" in str(key) for key in open_states
    )
    savings = tiles["savings_mtd_ytd"]["data"]
    assert "mtd" in savings and "ytd" in savings  # zeros when no C-05 runs

    for pending_id in ("autonomy_rate_trend", "no_touch_trend"):
        assert tiles[pending_id]["status"] == "pending_capture", pending_id

    csv = client.get(
        "/console/ops/portfolio/open_claims_by_state.csv",
        headers=_bearer("cm-token"),
    )
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.strip(), "CSV export must not be empty"

    blocked = client.get(
        "/console/ops/portfolio/autonomy_rate_trend.csv",
        headers=_bearer("cm-token"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "SERIES_BLOCKED_ON_INPUTS"

    auditor = client.get("/console/ops/portfolio", headers=_bearer("auditor-token"))
    assert auditor.status_code == 200


# --- admin surfaces -----------------------------------------------------------------


def test_ledger_search_roles_filters_and_hash_chain(env):
    client = env["client"]
    denied = client.get(
        "/console/ops/ledger?action=claim.created", headers=_bearer("officer-token")
    )
    assert denied.status_code == 403

    response = client.get(
        "/console/ops/ledger?action=claim.created", headers=_bearer("auditor-token")
    )
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert rows, "claim.created must be in the ledger"
    first = rows[0]
    for key in ("seq", "action", "actor", "row_hash"):
        assert key in first
    assert all(row["action"] == "claim.created" for row in rows)


def test_admin_packs_and_capabilities(env):
    client = env["client"]
    packs = client.get("/console/ops/packs", headers=_bearer("admin-token"))
    assert packs.status_code == 200, packs.text
    assert "motor" in str(packs.json())

    capabilities = client.get(
        "/console/ops/capabilities", headers=_bearer("cm-token")
    )
    assert capabilities.status_code == 200, capabilities.text
    listed = capabilities.json()
    assert listed, "PACKET-08 capability seed list must be visible"

    denied = client.get(
        "/console/ops/capabilities", headers=_bearer("officer-token")
    )
    assert denied.status_code == 403


def test_console_promote_keeps_packet_08_fail_closed_semantics(env):
    client = env["client"]
    capabilities = client.get(
        "/console/ops/capabilities", headers=_bearer("cm-token")
    ).json()["capabilities"]
    # Owner-amended for PACKET-16: permanent-L0-max capabilities (e.g.
    # assessment.mode_shadow) now exist; the #78 shadow-exit probe needs an
    # L0 row with promotion headroom, not merely the first L0 row.
    l0 = [
        row for row in capabilities
        if row["current_level"] == "L0" and row["max_level"] != "L0"
    ]
    assert l0, "PACKET-08 seeds L0 capabilities with promotion headroom"
    capability_id = l0[0]["id"]
    response = client.post(
        f"/console/ops/capabilities/{capability_id}/promote",
        json={
            "to_level": 1,
            "sign_offs": [{"actor": CM, "role": "claims_manager"}],
        },
        headers=_bearer("cm-token"),
    )
    # register #78: L0 -> L1 fails closed until the shadow-exit policy lands
    assert response.status_code in (409, 422), response.text
    assert "CRITERIA_NOT_MET" in response.text

    denied = client.post(
        f"/console/ops/capabilities/{capability_id}/promote",
        json={"to_level": 1, "sign_offs": []},
        headers=_bearer("officer-token"),
    )
    assert denied.status_code == 403


# --- scenario 2 ---------------------------------------------------------------------


def test_scenario_2_decline_visible_from_queue_360_and_portfolio(env):
    client, app = env["client"], env["app"]
    from claim_core.schemas import ClaimCreate

    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), AGENT
    )
    with app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET assigned_to = :actor WHERE id = :claim_id"),
            {"actor": OFFICER, "claim_id": claim.id},
        )
    # walk to a post-triage state; rule-linked guards record guards_pending
    # per PACKET-02 D-6 (register #24). In-process service calls: the installed
    # console ingress rejects every network X-Actor header (register #100), and
    # agents call public in-process engine interfaces in this phase.
    for target in ("TRIAGED", "AWAITING_DOCS"):
        app.state.claim_service.transition_claim(claim.id, target, {}, AGENT)

    declined = app.state.claim_service.decline_claim(claim.id, "other", AGENT)
    assert declined.approval_required, "post-triage decline must require approval"
    _drain(app)

    # queue visibility
    queue = client.get(
        f"/reviews?scope=pool&claim_id={claim.id}", headers=_bearer("cm-token")
    )
    assert queue.status_code == 200
    items = [
        item
        for item in queue.json()["items"]
        if item["type"] == "EXCEPTION"
        and item["subtype"] == "decline_approval_required"
        and item["status"] == "open"
    ]
    assert len(items) == 1
    item_id = items[0]["id"]

    resolved = client.post(
        f"/reviews/{item_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "EXCEPTION@1",
            "payload": {
                "capability_id": "triage.decline_draft",
                "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
            },
        },
        headers=_bearer("cm-token"),
    )
    assert resolved.status_code == 200, resolved.text
    _drain(app)

    # 360 visibility
    full = client.get(
        f"/console/claims/{claim.id}/360", headers=_bearer("officer-token")
    )
    assert full.status_code == 200, full.text
    assert full.json()["claim"]["status"] == "DECLINED"

    # portfolio visibility
    tiles = {
        tile["series_id"]: tile
        for tile in client.get(
            "/console/ops/portfolio", headers=_bearer("cm-token")
        ).json()["tiles"]
    }
    assert "DECLINED" in str(tiles["open_claims_by_state"]["data"])


# --- surface hygiene ----------------------------------------------------------------


def test_ops_openapi_write_surface_is_exactly_pinned(env):
    client = env["client"]
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    import re

    def normalise(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    ops_writes = []
    for path, spec in paths.items():
        if not path.startswith("/console/ops/"):
            continue
        for method in ("post", "put", "patch", "delete"):
            if method in spec:
                ops_writes.append(f"{method.upper()} {normalise(path)}")
    assert sorted(ops_writes) == sorted(
        [
            "POST /console/ops/sla-board/escalate",
            "POST /console/ops/notifications/{}/read",
            "POST /console/ops/capabilities/{}/promote",
            # PACKET-21 §11 / register #297: clearing a qualified projection
            # circuit breaker is an admin action on this surface, not a ninth
            # Claim-360 route. It is still a write, so it stays pinned here.
            "POST /console/ops/projection-circuits/{}/clear",
        ]
    ), ops_writes
