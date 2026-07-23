"""PACKET-20 acceptance — PRD-09 projection substrate and paste-assist mode.

Protected (CODEOWNERS): the builder may not weaken this file once merged.
Contract per docs/packets/PACKET-20_projection_paste_assist.md §12. No adapter,
Playwright session, browser, screenshot, service account, or target-system call
is permitted anywhere in this suite: every executable mechanic runs against a
synthetic fixture click path that is explicitly *not* the production
configuration.

The production motor catalogue is deliberately non-executable — no ICON or EDMS
path has been captured — so each mechanic is proved twice: blocked on the live
pack, and exercised on the fixture pack.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:projection"
OFFICER = "user:01HP20OFFICER00000000AAAAA"
MANAGER = "user:01HP20MANAGER00000000AAAAA"
ASSISTANT = "user:01HP20ASSTMANAGER0000AAAAA"
AUDITOR = "user:01HP20AUDITOR00000000AAAAA"
ROLES = {
    OFFICER: "claims_officer",
    MANAGER: "claims_manager",
    ASSISTANT: "asst_claims_manager",
    AUDITOR: "auditor",
}
TENANT = "33333333-3333-3333-3333-333333333333"
OIDS = {
    OFFICER: "aaaaaaaa-2020-4aaa-aaaa-aaaaaaaaaaaa",
    MANAGER: "bbbbbbbb-2020-4bbb-bbbb-bbbbbbbbbbbb",
    ASSISTANT: "cccccccc-2020-4ccc-cccc-cccccccccccc",
    AUDITOR: "dddddddd-2020-4ddd-dddd-dddddddddddd",
}
IDENTITIES = {f"{TENANT}:{oid}": actor for actor, oid in OIDS.items()}

T0 = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)  # 11:00 EAT

POLICY_NUMBER = "POL-20-0001"
INSURED_NAME = "Grace Wanjiru"
LOSS_DATE = "2026-07-01"
RESERVE_TOTAL = 14_265_600  # KES 142,656.00 in integer cents
ICON_CLAIM_NO = "ICON-004521"

FIFTEEN_OPERATIONS = (
    "icon.policy_read",
    "icon.claim_register",
    "icon.reserve_create",
    "icon.reserve_breakdown",
    "icon.reserve_adjust",
    "icon.assessor_payment_request",
    "icon.note_entry",
    "icon.claim_details_report",
    "icon.salvage_register",
    "icon.payment_voucher",
    "edms.general_payments",
    "edms.claims_workflow",
    "edms.attach_and_tag",
    "edms.claim_payment",
    "edms.payment_workflow",
)
LEGACY_BARE_IDS = (
    "icon.claim_register",
    "icon.reserve_adjust",
    "icon.assessor_payment_request",
    "icon.payment_voucher",
    "edms.attach_and_tag",
    "edms.claims_workflow",
)


class FixedClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0) -> None:
        self.now += timedelta(seconds=seconds)


class FakeVerifier:
    """Pinned TokenVerifier seam. No live Entra tenant is contacted."""

    def verify(self, token: str):
        from review_queue.auth import TokenClaims, TokenVerificationError

        oid = OIDS.get(token)
        if oid is None:
            raise TokenVerificationError("unknown fixture token")
        return TokenClaims(tid=TENANT, oid=oid)


def _h(actor: str = OFFICER, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {actor}", **extra}


@dataclass
class Env:
    app: Any
    client: Any
    clock: FixedClock
    pack: pathlib.Path


# --- synthetic fixture pack ----------------------------------------------------------

CLICK_PATH = {
    "operation": "icon.claim_register",
    "version": "1.1.0",
    "status": "live",
    "preconditions": [{"assert": "logged_in"}],
    "screens": [
        {"id": "claim_details", "label": "Claim details", "order": 1},
        {"id": "reserve", "label": "Reserve", "order": 2},
    ],
    "steps": [
        {
            "id": "s1",
            "screen": "claim_details",
            "action": "fill",
            "selector": "#policyNo",
            "value": "{policy.number}",
            "external_encoding": "raw",
            "paste_assist": {"label": "Policy number", "copy": True},
        },
        {
            "id": "s2",
            "screen": "claim_details",
            "action": "fill",
            "selector": "#insuredName",
            "value": "{parties.insured.name}",
            "external_encoding": "raw",
            "paste_assist": {"label": "Insured name", "copy": True},
        },
        {
            "id": "s3",
            "screen": "claim_details",
            "action": "fill",
            "selector": "#lossDate",
            "value": "{loss.date}",
            "external_encoding": "iso",
            "paste_assist": {"label": "Loss date", "copy": True},
        },
        {
            "id": "s4",
            "screen": "reserve",
            "action": "fill",
            "selector": "#reserveTotal",
            "value": "{reserve.total}",
            "external_encoding": "shillings",
            "paste_assist": {"label": "Reserve total", "copy": True},
        },
        {
            "id": "s5",
            "screen": "reserve",
            "action": "click",
            "selector": "#register",
        },
    ],
    "readback": [
        {
            "capture": "claim_number",
            "label": "ICON claim number",
            "into": "external.icon.claim_no",
            "assert_format": "icon_claim_no_regex",
        }
    ],
    "validators": {
        "icon_claim_no_regex": {"status": "live", "pattern": r"^ICON-[0-9]{6}$"}
    },
    "failure_policy": "screenshot_always, halt_on_selector_miss, no_guessing",
}
LIVE_ROW = {
    "id": "icon.claim_register",
    "version": "1.1.0",
    "system": "icon",
    "mode": "paste_assist",
    "status": "live",
    "blocked_on": None,
    "click_path_ref": "icon.claim_register@1.1.0.yaml",
    "owner_prd": "PRD-09",
}


def _fixture_pack(
    tmp_path: pathlib.Path,
    *,
    name: str = "fixture-pack",
    live: bool = True,
    click_path: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> pathlib.Path:
    """Copy the production motor pack and overlay a synthetic projection path."""

    pack = tmp_path / name / "motor"
    if pack.exists():
        shutil.rmtree(pack)
    pack.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MOTOR_PACK, pack)
    catalogue_path = pack / "projection" / "operations.yaml"
    catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    if live:
        overlay = dict(LIVE_ROW)
        overlay.update(row or {})
        catalogue["operations"] = [
            overlay if entry["id"] == overlay["id"] else entry
            for entry in catalogue["operations"]
        ]
        definition = json.loads(json.dumps(click_path or CLICK_PATH))
        (pack / "projection" / str(overlay["click_path_ref"])).write_text(
            yaml.safe_dump(definition, sort_keys=False), encoding="utf-8"
        )
    catalogue_path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
    return pack


def _build(tmp_path: pathlib.Path, name: str, *, pack: pathlib.Path | None = None) -> Env:
    # I001 suppressed: package becomes first-party only after implementation.
    from fastapi.testclient import TestClient  # noqa: I001

    from agent_runtime import build_agent_runtime
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from projection_agent import build_projection_agent
    from review_queue import build_review_queue, install_console

    pack = pack or MOTOR_PACK
    clock = FixedClock()
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    app = create_app(database_url, clock=clock)
    build_cop_runtime(app, pack_paths=[pack])
    build_eval_harness(app)
    build_review_queue(app, roles=dict(ROLES))
    build_agent_runtime(app)
    install_console(
        app,
        verifier=FakeVerifier(),
        identities=dict(IDENTITIES),
        roles=dict(ROLES),
    )
    build_projection_agent(app, operation_root=pack / "projection")
    return Env(app, TestClient(app), clock, pack)


# --- seeding -------------------------------------------------------------------------


def _drain(app, cycles: int = 24) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _claim(env: Env, *, status: str = "REPORT_RECEIVED") -> str:
    from claim_core.schemas import ClaimCreate

    claim = env.app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), AGENT
    )
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE claims SET status = :status WHERE id = :id"),
            {"status": status, "id": claim.id},
        )
    return claim.id


def _write(env: Env, claim_id: str, values: dict[str, tuple[Any, str]], *, actor=OFFICER) -> None:
    from claim_core import FieldWrite

    env.app.state.claim_service.write_fields(
        claim_id,
        [
            FieldWrite(
                path=path,
                value=value,
                value_type=value_type,
                source_type="human",
                source_ref={"user_id": actor},
                verification_state="human_verified",
            )
            for path, (value, value_type) in values.items()
        ],
        actor,
    )


def _seed(env: Env, *, status: str = "REPORT_RECEIVED") -> str:
    claim_id = _claim(env, status=status)
    _write(
        env,
        claim_id,
        {
            "policy.number": (POLICY_NUMBER, "string"),
            "parties.insured.name": (INSURED_NAME, "string"),
            "loss.date": (LOSS_DATE, "date"),
            "reserve.total": (RESERVE_TOTAL, "money"),
        },
    )
    return claim_id


def _events(env: Env, event_type: str, claim_id: str | None = None) -> list[dict[str, Any]]:
    statement = "SELECT id, claim_id, payload FROM events WHERE type = :type"
    parameters: dict[str, Any] = {"type": event_type}
    if claim_id is not None:
        statement += " AND claim_id = :claim_id"
        parameters["claim_id"] = claim_id
    with env.app.state.engine.connect() as connection:
        rows = connection.execute(text(statement + " ORDER BY seq"), parameters).mappings()
        return [{**dict(row), "payload": _loads(row["payload"])} for row in rows]


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _projections(env: Env) -> list[dict[str, Any]]:
    with env.app.state.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT * FROM projections ORDER BY created_at, id")
        ).mappings()
        return [dict(row) for row in rows]


def _request(env: Env, claim_id: str, operation: str):
    return env.app.state.projection_agent.request(
        claim_id=claim_id, operation=operation, actor=AGENT
    )


def _run_paste(
    env: Env,
    claim_id: str,
    projection_id: str,
    *,
    actor: str = OFFICER,
    readback: dict[str, str] | None = None,
    key: str = "confirm-1",
    seconds: int = 42,
):
    base = f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist"
    assert env.client.post(f"{base}/start", headers=_h(actor)).status_code == 200
    for group in ("claim_details", "reserve"):
        response = env.client.put(
            f"{base}/groups/{group}", json={"done": True}, headers=_h(actor)
        )
        assert response.status_code == 200, response.text
    env.clock.advance(seconds=seconds)
    return env.client.post(
        f"{base}/confirm",
        json={
            "attested": True,
            "readback": readback or {"external.icon.claim_no": ICON_CLAIM_NO},
        },
        headers=_h(actor, **{"Idempotency-Key": key}),
    )


@pytest.fixture()
def live_env(tmp_path):
    """The honest production catalogue: nothing is executable."""

    return _build(tmp_path, "prod")


@pytest.fixture()
def env(tmp_path):
    """The synthetic fixture pack, which is not the production configuration."""

    return _build(tmp_path, "fixture", pack=_fixture_pack(tmp_path))


# --- 1. migration --------------------------------------------------------------------


@pytest.mark.schema_isolated
def test_migration_0015_creates_the_exact_prd09_table_and_reverses(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/migration.db")
    config = Config(str(REPO / "platform" / "claim_core" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "platform" / "claim_core" / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0015_projections")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("projections")}
        assert set(columns) == {
            "id",
            "claim_id",
            "operation",
            "mode",
            "status",
            "payload",
            "readback",
            "divergence",
            "evidence",
            "attempts",
            "idempotency_key",
            "created_at",
            "completed_at",
        }
        for required in ("claim_id", "operation", "mode", "status", "payload"):
            assert columns[required]["nullable"] is False
        for optional in ("readback", "divergence", "evidence", "completed_at"):
            assert columns[optional]["nullable"] is True
        assert "0" in str(columns["attempts"]["default"])
        # No foreign keys and no invented index: the PRD-09 DDL has neither.
        assert inspector.get_foreign_keys("projections") == []
        unique = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("projections")
        } | {
            tuple(index["column_names"])
            for index in inspector.get_indexes("projections")
            if index["unique"]
        }
        assert ("idempotency_key",) in unique
        if engine.dialect.name == "postgresql":
            with engine.connect() as connection:
                data_type = connection.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = 'projections' AND column_name = 'payload'"
                    )
                ).scalar()
            assert data_type == "jsonb"
    finally:
        engine.dispose()

    command.downgrade(config, "0014_note_drafts")
    engine = create_engine(url)
    try:
        assert "projections" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


# --- 2/3. catalogue and capability ids ------------------------------------------------


def test_catalogue_registers_exactly_fifteen_operations_and_stays_honest(live_env):
    registry = live_env.app.state.projection_agent.operations
    assert registry.ids == FIFTEEN_OPERATIONS
    rows = {row["id"]: row for row in registry.catalogue()}
    assert all(row["mode"] == "paste_assist" for row in rows.values())
    assert rows["icon.reserve_adjust"]["blocked_on"] == "open-item-17"
    assert rows["icon.salvage_register"]["status"] == "blocked_on_inputs"
    assert rows["icon.salvage_register"]["owner_prd"] == "PRD-11"
    for payment in ("icon.payment_voucher", "edms.claim_payment", "edms.payment_workflow"):
        assert rows[payment]["status"] == "blocked_on_inputs"
        assert rows[payment]["owner_prd"] == "PRD-12"
    pending = {
        row["id"] for row in rows.values() if row["status"] == "pending_capture"
    }
    assert all(rows[row]["blocked_on"].startswith("open-item-") for row in pending)
    # No production operation is live: no click path has been captured.
    assert not any(row["status"] == "live" for row in rows.values())


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ("unknown_id", "unknown operation id"),
        ("missing_row", "missing"),
        ("duplicate_row", "duplicate operation id"),
        ("system_mismatch", "does not belong to system"),
        ("bad_version", "major.minor.patch"),
        ("unknown_key", "keys are invalid"),
        ("live_without_path", "live without a click path"),
        ("blocked_without_blocker", "blocked without a blocker"),
        ("bad_sampling", "rate_percent"),
        ("path_traversal", "escapes the operation root"),
    ],
)
def test_catalogue_defects_fail_startup(tmp_path, mutation, fragment):
    from projection_agent.config import OperationConfigError, OperationRegistry

    pack = _fixture_pack(tmp_path, name=f"broken-{mutation}", live=False)
    path = pack / "projection" / "operations.yaml"
    catalogue = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = catalogue["operations"]
    if mutation == "unknown_id":
        rows[0]["id"] = "icon.not_a_real_operation"
    elif mutation == "missing_row":
        rows.pop()
    elif mutation == "duplicate_row":
        rows.append(dict(rows[0]))
    elif mutation == "system_mismatch":
        rows[0]["system"] = "edms"
    elif mutation == "bad_version":
        rows[0]["version"] = "1.0"
    elif mutation == "unknown_key":
        rows[0]["retry_after"] = 5
    elif mutation == "live_without_path":
        rows[1].update(status="live", blocked_on=None, click_path_ref=None)
    elif mutation == "blocked_without_blocker":
        rows[0]["blocked_on"] = None
    elif mutation == "bad_sampling":
        catalogue["paste_readback_sampling"]["rate_percent"] = 250
    elif mutation == "path_traversal":
        rows[1].update(
            status="live", blocked_on=None, click_path_ref="../../../etc/hosts"
        )
    path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
    with pytest.raises(OperationConfigError) as error:
        OperationRegistry(pack / "projection")
    assert fragment in str(error.value)


def test_live_row_requires_a_same_operation_same_version_click_path(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    stale = json.loads(json.dumps(CLICK_PATH))
    stale["version"] = "1.0.0"
    pack = _fixture_pack(tmp_path, name="stale-version", click_path=stale)
    with pytest.raises(OperationConfigError) as error:
        OperationRegistry(pack / "projection")
    assert "does not match" in str(error.value)

    wrong = json.loads(json.dumps(CLICK_PATH))
    wrong["operation"] = "icon.reserve_create"
    pack = _fixture_pack(tmp_path, name="wrong-operation", click_path=wrong)
    with pytest.raises(OperationConfigError) as error:
        OperationRegistry(pack / "projection")
    assert "declares operation" in str(error.value)


def test_operation_and_capability_ids_stay_distinct_with_pinned_levels(live_env):
    from eval_harness.policies import PROJECTION_CEILINGS, constitutional_ceiling

    registry = live_env.app.state.projection_agent.operations
    assert registry.capability_ids() == tuple(
        f"project.{operation}" for operation in FIFTEEN_OPERATIONS
    )
    assert not set(registry.ids) & set(registry.capability_ids())

    autonomy = live_env.app.state.eval_harness.autonomy
    with live_env.app.state.engine.connect() as connection:
        seeded = {
            row["id"]: row
            for row in connection.execute(
                text("SELECT id, current_level, max_level FROM capabilities")
            ).mappings()
        }
    # Fresh seeds carry only canonical ids; the provisional bare ones are gone.
    for legacy in LEGACY_BARE_IDS:
        assert legacy not in seeded
    for operation in FIFTEEN_OPERATIONS:
        capability = f"project.{operation}"
        assert capability in seeded
        assert seeded[capability]["max_level"] == PROJECTION_CEILINGS[capability]
        assert seeded[capability]["max_level"] == constitutional_ceiling(capability)
    # Paste-assist launches at L1; PRD-11/12 rows stay dark until their packet.
    assert autonomy.level("project.icon.claim_register") == "L1"
    assert autonomy.level("project.edms.claims_workflow") == "L1"
    for future in (
        "project.icon.salvage_register",
        "project.icon.payment_voucher",
        "project.edms.claim_payment",
        "project.edms.payment_workflow",
    ):
        assert autonomy.level(future) == "L0"
    # Money-adjacent ceilings are constitution, not pack data.
    assert constitutional_ceiling("project.icon.reserve_create") == "L3"
    assert constitutional_ceiling("project.edms.general_payments") == "L3"
    assert constitutional_ceiling("project.icon.payment_voucher") == "L2"


def test_pack_policy_may_tighten_a_projection_ceiling_but_never_widen_it(tmp_path):
    from eval_harness.policies import load_policies

    path = tmp_path / "policies.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "capabilities": [
                    {"id": "project.icon.payment_voucher", "max_level": "L1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_policies(path)[0].max_level == "L1"
    path.write_text(
        yaml.safe_dump(
            {
                "capabilities": [
                    {"id": "project.icon.payment_voucher", "max_level": "L3"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="widens constitution"):
        load_policies(path)


def test_legacy_bare_capability_with_durable_evidence_fails_closed(tmp_path):
    from projection_agent import LegacyProjectionCapabilityInUse, _retire_legacy_capabilities

    env = _build(tmp_path, "legacy")
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO autonomy_changes (id, capability_id, from_level, to_level, "
                "reason, evidence, occurred_at) VALUES "
                "(:id, :capability, 'L0', 'L1', 'manual', :evidence, :occurred_at)"
            ),
            {
                "id": "01HP20LEGACYCHANGE0000AAAA",
                "capability": "icon.claim_register",
                "evidence": json.dumps({"note": "historical PACKET-08 seed"}),
                "occurred_at": T0,
            },
        )
    with pytest.raises(LegacyProjectionCapabilityInUse) as error:
        _retire_legacy_capabilities(env.app.state.engine)
    assert "LEGACY_PROJECTION_CAPABILITY_IN_USE" in str(error.value)


# --- 4. PACKET-17 reserve-event compatibility ----------------------------------------


def _reserve_pack(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """A fixture whose live operation is `icon.reserve_create`, not registration."""

    definition = json.loads(json.dumps(CLICK_PATH))
    definition["operation"] = "icon.reserve_create"
    definition["screens"] = [{"id": "reserve", "label": "Reserve", "order": 1}]
    definition["steps"] = [
        {
            "id": "r1",
            "screen": "reserve",
            "action": "fill",
            "selector": "#reserveTotal",
            "value": "{reserve.total}",
            "external_encoding": "shillings",
            "paste_assist": {"label": "Reserve total", "copy": True},
        }
    ]
    definition["readback"] = []
    row = dict(
        LIVE_ROW,
        id="icon.reserve_create",
        click_path_ref="icon.reserve_create@1.1.0.yaml",
    )
    return _fixture_pack(tmp_path, name=name, click_path=definition, row=row)


def _seed_c02(env: Env, claim_id: str, *, run_id: str, total: int) -> None:
    from claim_core import FieldWrite

    with env.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO calc_runs (id, calc_id, version, inputs, output, claim_id, ts, "
                "pack_id, pack_version, status, missing_inputs, actor) VALUES "
                "(:id, 'C-02', '1.0.0', :inputs, :output, :claim_id, :ts, 'motor', "
                "'motor@1.0.0', 'executed', :missing, :actor)"
            ),
            {
                "id": run_id,
                "inputs": json.dumps({"estimate": total}),
                "output": json.dumps(total),
                "claim_id": claim_id,
                "ts": T0,
                "missing": json.dumps([]),
                "actor": AGENT,
            },
        )
    env.app.state.claim_service.write_fields(
        claim_id,
        [
            FieldWrite(
                path="reserve.total",
                value=total,
                value_type="money",
                source_type="calc",
                source_ref={"calc_id": "C-02", "calc_run_id": run_id},
                verification_state="system_confirmed",
            )
        ],
        AGENT,
    )


def _emit(env: Env, claim_id: str, payload: dict[str, Any]) -> str:
    with Session(env.app.state.engine) as session:
        event = env.app.state.record_event(
            session,
            claim_id=claim_id,
            event_type="projection.requested",
            payload=payload,
            actor="agent:assessment",
            correlation_id=None,
        )
        session.commit()
        return event.id


def test_legacy_reserve_request_maps_only_to_reserve_create_and_replays_once(tmp_path):
    env = _build(tmp_path, "reserve", pack=_reserve_pack(tmp_path, "reserve-pack"))
    claim_id = _claim(env, status="REGISTERED")
    run_id = "01HP20C02RUN0000000000AAAA"
    _seed_c02(env, claim_id, run_id=run_id, total=RESERVE_TOTAL)
    payload = {
        "claim_id": claim_id,
        "calc_run_id": run_id,
        "reserve_total": RESERVE_TOTAL,
    }
    _emit(env, claim_id, payload)
    _drain(env.app)
    rows = _projections(env)
    assert len(rows) == 1
    assert rows[0]["operation"] == "icon.reserve_create"
    assert rows[0]["mode"] == "paste_assist"
    # Exact redelivery and a rebuild backfill both create nothing further.
    _emit(env, claim_id, payload)
    _drain(env.app)
    env.app.state.projection_agent.backfill(actor="system")
    assert len(_projections(env)) == 1
    # C-03 is blocked, so no reserve breakdown is ever fabricated.
    assert all(row["operation"] != "icon.reserve_breakdown" for row in _projections(env))


def test_legacy_reserve_request_is_never_trusted_without_durable_sources(tmp_path):
    env = _build(tmp_path, "reserve-bad", pack=_reserve_pack(tmp_path, "reserve-bad-pack"))
    claim_id = _claim(env, status="REGISTERED")
    run_id = "01HP20C02RUN0000000000BBBB"
    _seed_c02(env, claim_id, run_id=run_id, total=RESERVE_TOTAL)
    # A tampered total that matches neither the executed run nor the field.
    _emit(
        env,
        claim_id,
        {"claim_id": claim_id, "calc_run_id": run_id, "reserve_total": 99_999_900},
    )
    # A run id that does not exist.
    _emit(
        env,
        claim_id,
        {
            "claim_id": claim_id,
            "calc_run_id": "01HP20MISSINGRUN000000AAAA",
            "reserve_total": RESERVE_TOTAL,
        },
    )
    _drain(env.app)
    assert _projections(env) == []


def test_unknown_operation_in_a_request_event_is_rejected_and_never_stored(env):
    claim_id = _seed(env)
    _emit(env, claim_id, {"operation": "icon.not_registered", "claim_id": claim_id})
    _drain(env.app)
    assert _projections(env) == []


# --- 5. idempotency -------------------------------------------------------------------


def test_snapshot_identity_governs_idempotency(env):
    claim_id = _seed(env)
    first = _request(env, claim_id, "icon.claim_register")
    assert first.status == "created"
    again = _request(env, claim_id, "icon.claim_register")
    assert again.status == "exists"
    assert again.projection_id == first.projection_id
    assert len(_projections(env)) == 1

    row = _projections(env)[0]
    payload = _loads(row["payload"])
    assert row["idempotency_key"] == (
        f"{claim_id}:icon.claim_register:{payload['snapshot_hash']}"
    )
    # The hash is SHA-256 over sorted compact JSON of definition + fields only.
    material = {
        "operation_definition": payload["operation_definition"],
        "fields": payload["fields"],
    }
    assert payload["snapshot_hash"] == hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()

    # A new field version is material, so a corrected input creates a new row.
    _write(env, claim_id, {"policy.number": ("POL-20-0002", "string")})
    third = _request(env, claim_id, "icon.claim_register")
    assert third.status == "created"
    assert third.projection_id != first.projection_id
    assert len(_projections(env)) == 2


def test_a_changed_operation_definition_version_creates_a_new_projection(tmp_path):
    env = _build(tmp_path, "v1", pack=_fixture_pack(tmp_path, name="v1-pack"))
    claim_id = _seed(env)
    first = _request(env, claim_id, "icon.claim_register")
    assert first.status == "created"
    before = _projections(env)[0]

    bumped = json.loads(json.dumps(CLICK_PATH))
    bumped["version"] = "1.2.0"
    pack = _fixture_pack(
        tmp_path,
        name="v1-pack",
        click_path=bumped,
        row={"version": "1.2.0", "click_path_ref": "icon.claim_register@1.2.0.yaml"},
    )
    rebuilt = _build(tmp_path, "v1", pack=pack)
    second = rebuilt.app.state.projection_agent.request(
        claim_id=claim_id, operation="icon.claim_register", actor=AGENT
    )
    assert second.status == "created"
    assert second.projection_id != first.projection_id
    rows = _projections(rebuilt)
    assert len(rows) == 2
    # The earlier immutable snapshot is untouched by the configuration change.
    assert _projections(rebuilt)[0]["payload"] == before["payload"]


# --- 6. PII ---------------------------------------------------------------------------


def test_projection_payload_never_stores_plaintext_pii_and_logs_each_decrypt(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    row = _projections(env)[0]
    raw = row["payload"] if isinstance(row["payload"], str) else json.dumps(row["payload"])
    assert INSURED_NAME not in raw
    payload = json.loads(raw)
    insured = next(
        entry for entry in payload["fields"] if entry["path"] == "parties.insured.name"
    )
    assert set(insured["value"]) == {"__enc__"}
    assert insured["value"]["__enc__"]["alg"] == "AES-256-GCM"

    before = len(_events(env, "pii.decrypted", claim_id))
    response = env.client.get(
        f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist",
        headers=_h(OFFICER),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    rows = response.json()["groups"][0]["fields"]
    insured_row = next(row for row in rows if row["path"] == "parties.insured.name")
    assert insured_row["copy_value"] == INSURED_NAME
    decrypts = _events(env, "pii.decrypted", claim_id)
    assert len(decrypts) == before + 1
    assert decrypts[-1]["payload"]["field_path"] == "parties.insured.name"


# --- 7. blocked production operations -------------------------------------------------


def test_pending_production_operation_is_visible_and_blocks_without_a_row(live_env, tmp_path):
    claim_id = _seed(live_env)
    result = _request(live_env, claim_id, "icon.claim_register")
    assert result.status == "blocked_on_inputs"
    assert result.blocked_on == "open-item-3"
    assert _projections(live_env) == []
    surface = live_env.app.state.projection_agent.claim_surface(claim_id, actor=OFFICER)
    row = next(row for row in surface["operations"] if row["id"] == "icon.claim_register")
    assert row["status"] == "pending_capture"
    assert row["blocked_on"] == "open-item-3"
    assert surface["projections"] == []

    # PRD-11/PRD-12 rows are registered but non-executable in this packet.
    for future in ("icon.salvage_register", "edms.payment_workflow"):
        blocked = _request(live_env, claim_id, future)
        assert blocked.status == "blocked_on_inputs"
    assert _projections(live_env) == []


def test_a_blocked_request_event_creates_the_row_after_capture(tmp_path):
    """The immutable source event is not lost: the next backfill creates the row."""

    env = _build(tmp_path, "capture", pack=_fixture_pack(tmp_path, name="capture-pack", live=False))
    claim_id = _seed(env)
    event_id = _emit(env, claim_id, {"operation": "icon.claim_register", "claim_id": claim_id})
    _drain(env.app)
    assert _projections(env) == []

    # The builder's own history backfill creates the row once the definition is
    # captured; the queued event was never mutated to make that possible.
    captured = _build(
        tmp_path, "capture", pack=_fixture_pack(tmp_path, name="capture-pack")
    )
    rows = _projections(captured)
    assert len(rows) == 1
    payload = _loads(rows[0]["payload"])
    assert payload["source_event_id"] == event_id
    # Every later backfill is idempotent.
    assert captured.app.state.projection_agent.backfill(actor="system") == 0
    assert len(_projections(captured)) == 1


def test_under_verified_and_missing_inputs_block_rather_than_render_a_blank(env):
    claim_id = _claim(env)
    blocked = _request(env, claim_id, "icon.claim_register")
    assert blocked.status == "blocked_on_inputs"
    assert blocked.blocked_on.startswith("field_missing:")
    assert _projections(env) == []


# --- 8/9. field strip and money ------------------------------------------------------


def test_strip_renders_click_path_order_with_exact_clipboard_strings(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    view = env.client.get(
        f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist",
        headers=_h(OFFICER),
    ).json()
    assert [group["id"] for group in view["groups"]] == ["claim_details", "reserve"]
    assert [group["label"] for group in view["groups"]] == ["Claim details", "Reserve"]
    assert [row["step_id"] for row in view["groups"][0]["fields"]] == ["s1", "s2", "s3"]
    # The non-copy click step never appears on the strip.
    assert [row["step_id"] for row in view["groups"][1]["fields"]] == ["s4"]
    values = {
        row["step_id"]: row["copy_value"]
        for group in view["groups"]
        for row in group["fields"]
    }
    assert values["s1"] == POLICY_NUMBER
    assert values["s2"] == INSURED_NAME
    assert values["s3"] == LOSS_DATE
    # Exact decimal division by 100: no rounding, commas, or currency prefix.
    assert values["s4"] == "142656.00"
    assert view["readback_fields"] == [
        {
            "label": "ICON claim number",
            "path": "external.icon.claim_no",
            "required": True,
            "format_status": "live",
            "blocked_on": None,
        }
    ]
    assert view["attestation_text"] == "I entered the values exactly as shown."


def test_money_without_a_declared_target_unit_blocks(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry
    from projection_agent.paste import SnapshotBlocked, encode_copy_value

    undeclared = json.loads(json.dumps(CLICK_PATH))
    for step in undeclared["steps"]:
        if step["id"] == "s4":
            step.pop("external_encoding")
    pack = _fixture_pack(tmp_path, name="money-pack", click_path=undeclared)
    with pytest.raises(OperationConfigError) as error:
        OperationRegistry(pack / "projection")
    assert "must declare an external_encoding" in str(error.value)

    # The runtime encoder refuses cents-into-shillings for the same reason.
    with pytest.raises(SnapshotBlocked):
        encode_copy_value(RESERVE_TOTAL, value_type="money", encoding="raw")
    assert (
        encode_copy_value(RESERVE_TOTAL, value_type="money", encoding="shillings")
        == "142656.00"
    )
    assert encode_copy_value(RESERVE_TOTAL, value_type="money", encoding="cents") == str(
        RESERVE_TOTAL
    )
    assert encode_copy_value(1_500_50, value_type="money", encoding="shillings") == "1500.50"


def test_generated_and_value_map_bindings_are_named_blockers_not_defaults(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    for value in ("{generated.loss_description}", '{rule:R-05 ? "A" : "B"}'):
        guessed = json.loads(json.dumps(CLICK_PATH))
        guessed["steps"][0]["value"] = value
        pack = _fixture_pack(tmp_path, name="guessed-pack", click_path=guessed)
        with pytest.raises(OperationConfigError) as error:
            OperationRegistry(pack / "projection")
        assert "explicitly declared" in str(error.value)


def test_readback_outside_the_external_dictionary_is_rejected(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    invented = json.loads(json.dumps(CLICK_PATH))
    invented["readback"][0]["into"] = "external.icon.not_a_field"
    pack = _fixture_pack(tmp_path, name="readback-pack", click_path=invented)
    with pytest.raises(OperationConfigError) as error:
        OperationRegistry(pack / "projection")
    assert "outside the external-field dictionary" in str(error.value)


# --- 10/11. lifecycle -----------------------------------------------------------------


def test_start_is_idempotent_groups_are_reversible_and_the_clock_starts_explicitly(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    base = f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist"

    # A read never starts the clock.
    env.clock.advance(seconds=100)
    assert env.client.get(base, headers=_h(OFFICER)).json()["started_at"] is None
    assert _projections(env)[0]["status"] == "queued"

    started = env.client.post(f"{base}/start", headers=_h(OFFICER)).json()
    assert started["status"] == "executing"
    first_start = started["started_at"]
    env.clock.advance(seconds=30)
    # Another authorised actor repeating start does not reset the clock.
    repeat = env.client.post(f"{base}/start", headers=_h(MANAGER)).json()
    assert repeat["started_at"] == first_start
    assert repeat["elapsed_seconds"] == 30

    assert env.client.put(
        f"{base}/groups/claim_details", json={"done": True}, headers=_h(OFFICER)
    ).json()["groups"][0]["done"] is True
    assert env.client.put(
        f"{base}/groups/claim_details", json={"done": False}, headers=_h(OFFICER)
    ).json()["groups"][0]["done"] is False
    payload_before = _projections(env)[0]["payload"]
    env.client.put(f"{base}/groups/reserve", json={"done": True}, headers=_h(OFFICER))
    # Group toggles never touch the immutable snapshot.
    assert _projections(env)[0]["payload"] == payload_before
    assert _events(env, "projection.completed", claim_id) == []


def test_confirm_requires_groups_attestation_and_the_exact_declared_readback(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    base = f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist"
    env.client.post(f"{base}/start", headers=_h(OFFICER))
    headers = _h(OFFICER, **{"Idempotency-Key": "k1"})

    incomplete = env.client.post(
        f"{base}/confirm",
        json={"attested": True, "readback": {"external.icon.claim_no": ICON_CLAIM_NO}},
        headers=headers,
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["code"] == "PROJECTION_GROUPS_INCOMPLETE"

    for group in ("claim_details", "reserve"):
        env.client.put(f"{base}/groups/{group}", json={"done": True}, headers=_h(OFFICER))

    unattested = env.client.post(
        f"{base}/confirm",
        json={"attested": "true", "readback": {"external.icon.claim_no": ICON_CLAIM_NO}},
        headers=headers,
    )
    assert unattested.status_code == 422
    assert unattested.json()["code"] == "ATTESTATION_REQUIRED"

    extra = env.client.post(
        f"{base}/confirm",
        json={
            "attested": True,
            "readback": {
                "external.icon.claim_no": ICON_CLAIM_NO,
                "external.edms.folder_ref": "EDMS-1",
            },
        },
        headers=headers,
    )
    assert extra.status_code == 422
    assert extra.json()["code"] == "READBACK_KEY_UNKNOWN"

    malformed = env.client.post(
        f"{base}/confirm",
        json={"attested": True, "readback": {"external.icon.claim_no": "ICON-BAD"}},
        headers=headers,
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "READBACK_FORMAT_INVALID"

    missing = env.client.post(
        f"{base}/confirm", json={"attested": True, "readback": {}}, headers=headers
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "READBACK_REQUIRED"

    # Nothing above moved the projection or wrote a field.
    assert _projections(env)[0]["status"] == "executing"
    assert _events(env, "projection.completed", claim_id) == []


def test_confirm_is_request_idempotent_and_a_completed_row_is_immutable(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    base = f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist"
    first = _run_paste(env, claim_id, projection_id, key="key-a")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "completed"
    assert first.json()["paste_seconds"] == 42

    repeat = env.client.post(
        f"{base}/confirm",
        json={"attested": True, "readback": {"external.icon.claim_no": ICON_CLAIM_NO}},
        headers=_h(OFFICER, **{"Idempotency-Key": "key-a"}),
    )
    assert repeat.status_code == 200
    assert repeat.json() == first.json()

    conflict = env.client.post(
        f"{base}/confirm",
        json={"attested": True, "readback": {"external.icon.claim_no": "ICON-000001"}},
        headers=_h(OFFICER, **{"Idempotency-Key": "key-a"}),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    completed = env.client.post(
        f"{base}/confirm",
        json={"attested": True, "readback": {"external.icon.claim_no": "ICON-000001"}},
        headers=_h(OFFICER, **{"Idempotency-Key": "key-b"}),
    )
    assert completed.status_code == 409
    assert completed.json()["code"] == "PROJECTION_ALREADY_COMPLETED"
    assert len(_events(env, "projection.completed", claim_id)) == 1


def test_roles_gate_the_strip_and_a_cross_claim_id_is_a_404(env):
    claim_id = _seed(env)
    other_claim = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    base = f"/console/claims/{claim_id}/projections/{projection_id}/paste-assist"

    assert env.client.get(base, headers=_h(AUDITOR)).status_code == 200
    denied = env.client.post(f"{base}/start", headers=_h(AUDITOR))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_ROLE"

    cross = env.client.get(
        f"/console/claims/{other_claim}/projections/{projection_id}/paste-assist",
        headers=_h(OFFICER),
    )
    assert cross.status_code == 404
    assert cross.json()["code"] == "PROJECTION_NOT_FOUND"

    anonymous = env.client.get(base)
    assert anonymous.status_code == 401


# --- 12/13/14. readback, crash safety, FSM -------------------------------------------


def test_completion_appends_the_canonical_field_and_updates_the_cache(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    assert _run_paste(env, claim_id, projection_id).status_code == 200
    _drain(env.app)

    snapshot = env.app.state.claim_service.snapshot_current_fields(
        claim_id, ["external.icon.claim_no"]
    )["external.icon.claim_no"]
    assert snapshot.value == ICON_CLAIM_NO
    assert snapshot.source_type == "projection_readback"
    assert snapshot.verification_state == "system_confirmed"
    assert snapshot.source_ref["projection_id"] == projection_id
    assert snapshot.source_ref["operation"] == "icon.claim_register"
    assert snapshot.source_ref["operation_version"] == "1.1.0"
    assert snapshot.source_ref["attested_by"] == OFFICER

    with env.app.state.engine.connect() as connection:
        refs = connection.execute(
            text("SELECT external_refs FROM claims WHERE id = :id"), {"id": claim_id}
        ).scalar()
    refs = _loads(refs)
    assert refs["external.icon.claim_no"] == ICON_CLAIM_NO

    events = _events(env, "projection.completed", claim_id)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["readback_paths"] == ["external.icon.claim_no"]
    assert payload["paste_seconds"] == 42
    assert payload["attested_by"] == OFFICER


def test_a_kill_between_field_commit_and_completion_resumes_exactly_once(env, monkeypatch):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    service = env.app.state.projection_agent
    original = service.claims.protect_snapshot_value
    state = {"crashed": False}

    def crash_once(*args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("synthetic process kill after the field commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.claims, "protect_snapshot_value", crash_once)
    with pytest.raises(RuntimeError):
        _run_paste(env, claim_id, projection_id, key="crash")
    monkeypatch.undo()

    assert _projections(env)[0]["status"] == "verifying"
    with env.app.state.engine.connect() as connection:
        versions = connection.execute(
            text(
                "SELECT COUNT(*) FROM claim_fields WHERE claim_id = :id "
                "AND path = 'external.icon.claim_no'"
            ),
            {"id": claim_id},
        ).scalar()
    assert versions == 1
    assert _events(env, "projection.completed", claim_id) == []

    assert service.resume(actor="system") == 1
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        versions = connection.execute(
            text(
                "SELECT COUNT(*) FROM claim_fields WHERE claim_id = :id "
                "AND path = 'external.icon.claim_no'"
            ),
            {"id": claim_id},
        ).scalar()
    assert versions == 1
    assert _projections(env)[0]["status"] == "completed"
    assert len(_events(env, "projection.completed", claim_id)) == 1
    with env.app.state.engine.connect() as connection:
        refs = connection.execute(
            text("SELECT external_refs FROM claims WHERE id = :id"), {"id": claim_id}
        ).scalar()
    refs = _loads(refs)
    assert refs["external.icon.claim_no"] == ICON_CLAIM_NO


def test_a_human_verified_icon_claim_number_is_never_superseded(env):
    claim_id = _seed(env)
    _write(env, claim_id, {"external.icon.claim_no": ("ICON-111111", "string")})
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    response = _run_paste(env, claim_id, projection_id, key="conflict")
    assert response.status_code == 409
    assert response.json()["code"] == "PROJECTION_READBACK_CONFLICT"
    _drain(env.app)

    snapshot = env.app.state.claim_service.snapshot_current_fields(
        claim_id, ["external.icon.claim_no"]
    )["external.icon.claim_no"]
    assert snapshot.value == "ICON-111111"
    assert snapshot.verification_state == "human_verified"
    assert _projections(env)[0]["status"] == "verifying"
    exceptions = [
        event
        for event in _events(env, "review.created", claim_id)
        if event["payload"].get("subtype") == "projection_readback_conflict"
    ]
    assert len(exceptions) == 1
    with env.app.state.engine.connect() as connection:
        items = connection.execute(
            text(
                "SELECT COUNT(*) FROM review_items WHERE claim_id = :id "
                "AND subtype = 'projection_readback_conflict'"
            ),
            {"id": claim_id},
        ).scalar()
    assert items == 1


def test_icon_completion_owns_report_received_to_registered_and_nothing_else(env):
    claim_id = _seed(env, status="REPORT_RECEIVED")
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    assert _run_paste(env, claim_id, projection_id).status_code == 200
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM claims WHERE id = :id"), {"id": claim_id}
            ).scalar()
            == "REGISTERED"
        )
    transitions = [
        event
        for event in _events(env, "claim.status_changed", claim_id)
        if event["payload"]["to"] == "REGISTERED"
    ]
    assert len(transitions) == 1
    assert transitions[0]["payload"]["from"] == "REPORT_RECEIVED"

    # Replay is a no-op and never owns REGISTERED -> RESERVED.
    env.app.state.projection_agent.resume(actor="system")
    _drain(env.app)
    assert (
        len(
            [
                event
                for event in _events(env, "claim.status_changed", claim_id)
                if event["payload"]["to"] == "REGISTERED"
            ]
        )
        == 1
    )
    assert not any(
        event["payload"]["to"] == "RESERVED"
        for event in _events(env, "claim.status_changed", claim_id)
    )


def test_any_other_claim_state_is_not_auto_transitioned(env):
    claim_id = _seed(env, status="IN_ASSESSMENT")
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    assert _run_paste(env, claim_id, projection_id).status_code == 200
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM claims WHERE id = :id"), {"id": claim_id}
            ).scalar()
            == "IN_ASSESSMENT"
        )
    mismatches = [
        event
        for event in _events(env, "review.created", claim_id)
        if event["payload"].get("subtype") == "projection_state_mismatch"
    ]
    assert len(mismatches) == 1
    # The completed projection stays visible.
    assert _projections(env)[0]["status"] == "completed"


# --- 15. weekly paste readback sampling ----------------------------------------------


def test_the_weekly_sampler_uses_the_exact_deterministic_selector(env):
    from projection_agent.service import ProjectionService

    sampling = env.app.state.projection_agent.operations.sampling
    assert (sampling.rate_percent, sampling.day_of_week) == (10, "mon")
    assert (sampling.hour, sampling.minute, sampling.timezone) == (8, 0, "Africa/Nairobi")

    def selected(projection_id: str) -> bool:
        digest = hashlib.sha256(projection_id.encode("utf-8")).hexdigest()
        return int(digest, 16) % 100 < 10

    for candidate in ("01HP20SELECT000000000AAAA", "01HP20SELECT000000000BBBB"):
        assert (
            ProjectionService.selected_for_readback(candidate, rate_percent=10)
            == selected(candidate)
        )


def _complete_many(env: Env, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        claim_id = _seed(env)
        _write(env, claim_id, {"policy.number": (f"POL-20-{index:04d}", "string")})
        projection_id = _request(env, claim_id, "icon.claim_register").projection_id
        assert (
            _run_paste(
                env,
                claim_id,
                projection_id,
                readback={"external.icon.claim_no": f"ICON-{index:06d}"},
                key=f"key-{index}",
            ).status_code
            == 200
        )
        ids.append(projection_id)
    return ids


def test_sampling_selects_exactly_the_deterministic_ten_percent(env):
    service = env.app.state.projection_agent
    completed = _complete_many(env, 8)
    expected = {
        projection_id
        for projection_id in completed
        if service.selected_for_readback(projection_id, rate_percent=10)
    }

    report = service.sample_paste_readbacks()
    assert report["rate_percent"] == 10
    assert report["scanned"] == len(completed)
    assert report["created"] == len(expected)
    _drain(env.app)
    created = [
        event
        for event in _events(env, "review.created")
        if event["payload"].get("type") == "PASTE_READBACK_CHECK"
    ]
    # Exactly the deterministically selected ids; no unselected id creates one.
    assert {event["payload"]["projection_id"] for event in created} == expected

    # A repeat weekly scan, and a duplicate dispatch, create no second item.
    assert service.sample_paste_readbacks()["created"] == 0
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        items = connection.execute(
            text("SELECT COUNT(*) FROM review_items WHERE type = 'PASTE_READBACK_CHECK'")
        ).scalar()
    assert items == len(expected)


def test_the_configured_rate_governs_selection_and_the_payload_is_pii_safe(tmp_path):
    """Rate is pack data: at 100 every completed row is sampled, at 0 none is."""

    for rate, expected_all in ((100, True), (0, False)):
        pack = _fixture_pack(tmp_path, name=f"rate-{rate}-pack")
        catalogue_path = pack / "projection" / "operations.yaml"
        catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
        catalogue["paste_readback_sampling"]["rate_percent"] = rate
        catalogue_path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
        env = _build(tmp_path, f"rate-{rate}", pack=pack)
        completed = _complete_many(env, 2)
        report = env.app.state.projection_agent.sample_paste_readbacks()
        assert report["created"] == (len(completed) if expected_all else 0)
        _drain(env.app)
        created = [
            event
            for event in _events(env, "review.created")
            if event["payload"].get("type") == "PASTE_READBACK_CHECK"
        ]
        assert len(created) == (len(completed) if expected_all else 0)
        for event in created:
            payload = event["payload"]
            assert payload["capability_id"] == "project.icon.claim_register"
            assert payload["operation"] == "icon.claim_register"
            assert payload["readback_paths"] == ["external.icon.claim_no"]
            assert set(payload) == {
                "type",
                "projection_id",
                "operation",
                "capability_id",
                "snapshot_hash",
                "readback_paths",
            }
            # Ids, hashes, and path names only — never a copied or readback value.
            assert "ICON-0" not in json.dumps(payload)
            assert INSURED_NAME not in json.dumps(payload)


def test_the_weekly_beat_slot_is_registered_from_pack_data(env):
    from claim_core import celery_app
    from projection_agent.tasks import BEAT_ENTRY, TASK_NAME

    entry = celery_app.conf.beat_schedule[BEAT_ENTRY]
    assert entry["task"] == TASK_NAME
    # Celery numbers Sunday as 0, so Monday is 1.
    assert entry["schedule"].day_of_week == {1}
    assert entry["schedule"].hour == {8}
    assert entry["schedule"].minute == {0}
    assert entry["options"]["timezone"] == "Africa/Nairobi"


# --- 16. events, ledger, and leak checks ---------------------------------------------


def test_no_copied_value_or_readback_reaches_events_reviews_or_the_ledger(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    assert _run_paste(env, claim_id, projection_id).status_code == 200
    env.app.state.projection_agent.sample_paste_readbacks()
    _drain(env.app)

    with env.app.state.engine.connect() as connection:
        events = connection.execute(
            text("SELECT type, payload FROM events WHERE type LIKE 'projection.%' "
                 "OR type = 'review.created'")
        ).mappings()
        for row in events:
            body = row["payload"] if isinstance(row["payload"], str) else json.dumps(row["payload"])
            assert INSURED_NAME not in body
            assert ICON_CLAIM_NO not in body
            assert POLICY_NUMBER not in body
        ledger = connection.execute(
            text("SELECT action, detail FROM audit_ledger WHERE action LIKE 'projection.%'")
        ).mappings()
        actions = set()
        for row in ledger:
            actions.add(row["action"])
            body = row["detail"] if isinstance(row["detail"], str) else json.dumps(row["detail"])
            assert INSURED_NAME not in body
            assert ICON_CLAIM_NO not in body
        items = connection.execute(
            text("SELECT payload FROM review_items")
        ).mappings()
        for row in items:
            body = row["payload"] if isinstance(row["payload"], str) else json.dumps(row["payload"])
            assert ICON_CLAIM_NO not in body
    assert "projection.completed" in actions


def test_the_projection_event_catalogue_and_ledger_map_stay_closed(env):
    from claim_core.ledger import ACTION_MAP

    projection_actions = {
        key: value for key, value in ACTION_MAP.items() if key.startswith("projection.")
    }
    assert projection_actions == {
        "projection.requested": "projection.requested",
        "projection.completed": "projection.completed",
        "projection.failed": "projection.failed",
    }
    # `projection.diverged` belongs to PACKET-21 and is deliberately unmapped.
    assert "projection.diverged" not in ACTION_MAP


# --- regression guards ----------------------------------------------------------------


def test_no_adapter_executor_or_funds_transfer_verb_is_registered(env):
    runtime = env.app.state.agent_runtime
    # PACKET-20 registers no executor at all: paste-assist is authenticated human
    # work, and PACKET-21 registers the first external executor behind the gate.
    assert not any(
        action_type.startswith(("project.", "adapter.", "icon.", "edms."))
        for action_type in runtime.gate._executors
    )
    for forbidden in (
        "settlement.eft_transfer",
        "settlement.pay",
        "icon.payment_voucher.execute",
        "project.icon.payment_voucher.execute",
    ):
        with pytest.raises(ValueError):
            runtime.register_executor(forbidden, lambda action: None)
    registry = env.app.state.projection_agent.operations
    assert not any(
        keyword in operation
        for operation in registry.ids
        for keyword in ("transfer", "pay_out", "release_funds")
    )


def test_the_generated_openapi_exposes_five_routes_and_no_adapter_surface(env):
    document = env.app.app.openapi() if hasattr(env.app, "app") else env.app.openapi()
    projection_paths = {
        path: sorted(operations)
        for path, operations in document["paths"].items()
        if "/projections" in path
    }
    assert projection_paths == {
        "/console/claims/{claim_id}/projections": ["get"],
        "/console/claims/{claim_id}/projections/{projection_id}/paste-assist": ["get"],
        "/console/claims/{claim_id}/projections/{projection_id}/paste-assist/start": ["post"],
        "/console/claims/{claim_id}/projections/{projection_id}"
        "/paste-assist/groups/{group_id}": ["put"],
        "/console/claims/{claim_id}/projections/{projection_id}/paste-assist/confirm": [
            "post"
        ],
    }
    # No adapter route, and no officer endpoint that can supply a payload,
    # snapshot hash, field version, mode, or idempotency key material.
    surface = json.dumps(document)
    for forbidden in ("/adapters", "/rpa", "adapter_health", "snapshot_hash"):
        assert forbidden not in surface
    confirm = document["paths"][
        "/console/claims/{claim_id}/projections/{projection_id}/paste-assist/confirm"
    ]["post"]
    schema_name = confirm["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].rsplit("/", 1)[-1]
    body = document["components"]["schemas"][schema_name]
    assert set(body["properties"]) == {"attested", "readback"}
    assert body.get("additionalProperties") is False


def test_the_approval_pack_icon_note_entry_slot_stays_pending_and_empty():
    icon = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "icon.yaml").read_text(encoding="utf-8")
    )
    field_set = icon["field_sets"]["icon.note_entry"]
    assert field_set["status"] == "pending_capture"
    assert field_set["fields"] == []


def test_the_grader_map_registers_projection_readback_to_the_critical_gval(env):
    mapping = yaml.safe_load(
        (MOTOR_PACK / "eval" / "grader_map.yaml").read_text(encoding="utf-8")
    )
    row = mapping["output_types"]["projection_readback"]
    assert {"id": "G-VAL", "severity": "critical"} in row["graders"]
    assert "external.icon.claim_no" in row["field_paths"]
    assert set(env.app.state.eval_harness.graders.ids()) == {
        "G-VAL",
        "G-CITE",
        "G-CALC",
        "G-RULE",
        "G-SUM",
        "G-TPL",
        "G-NOTE",
        "G-COMM",
        "G-PROC",
    }


def test_a_projection_readback_field_is_graded_by_gval_before_it_counts(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id, "icon.claim_register").projection_id
    assert _run_paste(env, claim_id, projection_id).status_code == 200
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        runs = connection.execute(
            text(
                "SELECT grader_id, result, subject_ref FROM grader_runs "
                "WHERE claim_id = :id"
            ),
            {"id": claim_id},
        ).mappings()
        graded = [
            row
            for row in runs
            if row["grader_id"] == "G-VAL"
            and "external.icon.claim_no" in str(row["subject_ref"])
        ]
    assert len(graded) == 1
    assert graded[0]["result"] == "pass"
