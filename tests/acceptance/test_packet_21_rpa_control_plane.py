"""PACKET-21 acceptance — RPA runtime and zero-silent-divergence control plane.

Protected (CODEOWNERS): the builder may not weaken this file once merged.
Contract per docs/packets/PACKET-21_rpa_control_plane.md §17.

Every external-system mechanic here runs against a deterministic synthetic
target served inside the test process (`tests/fixtures/projection`). No ICON or
EDMS path, credential, endpoint, or failure signature has been captured, so the
production motor pack is proved *non-executable* and the fixture pack proves the
runtime. A green run means `synthetic_control_plane_green`, never `rpa_live`.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

AGENT = "agent:projection"
RUNNER_ACTOR = "agent:projection-runner"
OFFICER = "user:01HP21OFFICER00000000AAAAA"
MANAGER = "user:01HP21MANAGER00000000AAAAA"
ADMIN = "user:01HP21ADMIN0000000000AAAAA"
AUDITOR = "user:01HP21AUDITOR00000000AAAAA"
ROLES = {
    OFFICER: "claims_officer",
    MANAGER: "claims_manager",
    ADMIN: "admin",
    AUDITOR: "auditor",
}
TENANT = "44444444-4444-4444-4444-444444444444"
OIDS = {
    OFFICER: "aaaaaaaa-2121-4aaa-aaaa-aaaaaaaaaaaa",
    MANAGER: "bbbbbbbb-2121-4bbb-bbbb-bbbbbbbbbbbb",
    ADMIN: "cccccccc-2121-4ccc-cccc-cccccccccccc",
    AUDITOR: "dddddddd-2121-4ddd-dddd-dddddddddddd",
}
IDENTITIES = {f"{TENANT}:{oid}": actor for actor, oid in OIDS.items()}
T0 = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)

OPERATION = "edms.claims_workflow"
CAPABILITY = "project.edms.claims_workflow"


class FixedClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0) -> None:
        self.now += timedelta(seconds=seconds)


def _h(actor: str = OFFICER, **extra: str) -> dict[str, str]:
    """Console identity is a bearer token; `X-Actor` is refused at the edge."""

    return {"Authorization": f"Bearer {actor}", **extra}


@dataclass
class Env:
    app: Any
    client: Any
    clock: FixedClock
    pack: pathlib.Path
    target: Any
    control_plane: Any
    runner: Any


# --- fixture pack ---------------------------------------------------------------------


def _fixture_pack(
    tmp_path: pathlib.Path,
    *,
    name: str = "fixture-pack",
    click_path: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    drift: dict[str, Any] | None = None,
    sampling_rate: int | None = None,
) -> pathlib.Path:
    """Copy the production motor pack and overlay a synthetic RPA definition."""

    from fixtures.projection import EDMS_CLAIMS_WORKFLOW, EDMS_LIVE_ROW

    pack = tmp_path / name / "motor"
    if pack.exists():
        shutil.rmtree(pack)
    pack.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MOTOR_PACK, pack)
    projection = pack / "projection"
    catalogue_path = projection / "operations.yaml"
    catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    overlay = {**EDMS_LIVE_ROW, **(row or {})}
    catalogue["operations"] = [
        overlay if entry["id"] == overlay["id"] else entry
        for entry in catalogue["operations"]
    ]
    if sampling_rate is not None:
        catalogue["paste_readback_sampling"]["rate_percent"] = sampling_rate
    catalogue_path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
    definition = json.loads(json.dumps(click_path or EDMS_CLAIMS_WORKFLOW))
    (projection / str(overlay["click_path_ref"])).write_text(
        yaml.safe_dump(definition, sort_keys=False), encoding="utf-8"
    )
    if runtime is not None:
        (projection / "runtime.yaml").write_text(
            yaml.safe_dump(runtime, sort_keys=False), encoding="utf-8"
        )
    if drift is not None:
        (projection / "drift.yaml").write_text(
            yaml.safe_dump(drift, sort_keys=False), encoding="utf-8"
        )
    return pack


def _runtime_document(**overrides: Any) -> dict[str, Any]:
    from projection_agent.config import load_runtime_config

    base = yaml.safe_load(
        (MOTOR_PACK / "projection" / "runtime.yaml").read_text(encoding="utf-8")
    )
    assert load_runtime_config(MOTOR_PACK / "projection" / "runtime.yaml") is not None
    base["runner"].update(overrides)
    return base


# --- application ----------------------------------------------------------------------


def _probe_factory(target: Any) -> Any:
    from fixtures.projection import EDMS_FIELD_PATHS

    def probe(operation_id: str, keys: dict[str, Any]) -> dict[str, Any]:
        del operation_id
        if "drift_check" in keys:
            return {"value": target.values.get("#reserveShillings")}
        by_selector = {selector: step for step, _p, _e, selector in EDMS_FIELD_PATHS}
        records = [
            {by_selector[selector]: value for selector, value in record.items()
             if selector in by_selector}
            for record in target.records
        ]
        return {
            "matches": len(records),
            "record": records[0] if records else {},
            "outputs": {"folder_ref": target.reference} if records else {},
        }

    return probe


def _build(
    tmp_path: pathlib.Path,
    name: str,
    *,
    pack: pathlib.Path | None = None,
    authenticator: Any = None,
    level: str = "L3",
    with_adapter: bool = True,
) -> Env:
    # I001 suppressed: package becomes first-party only after implementation.
    from fastapi.testclient import TestClient  # noqa: I001

    from agent_runtime import build_agent_runtime
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from fixtures.projection import SyntheticTarget, build_fixture_adapter
    from fixtures.projection.click_path import FOLDER_REF
    from projection_agent import build_projection_agent
    from projection_agent.config import load_runtime_config
    from projection_agent.runner import RunnerClient
    from projection_agent.runner_api import InProcessControlPlane
    from notify import build_notify
    from review_queue import build_review_queue, install_console, install_ops

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
        verifier=_FixtureVerifier(),
        identities=dict(IDENTITIES),
        roles=dict(ROLES),
    )
    build_notify(app, roles=dict(ROLES))
    install_ops(app)
    if level is not None:
        with app.state.engine.begin() as connection:
            connection.execute(
                text("UPDATE capabilities SET current_level = :level WHERE id = :id"),
                {"level": level, "id": CAPABILITY},
            )
    target = SyntheticTarget(reference=FOLDER_REF)
    timeouts = load_runtime_config(pack / "projection" / "runtime.yaml").runner
    adapters: dict[str, Any] = {}
    if with_adapter:
        adapters["edms"] = build_fixture_adapter(
            "edms",
            target=target,
            timeouts=timeouts,
            clock=clock,
            probe=_probe_factory(target),
        )
    service = build_projection_agent(
        app,
        operation_root=pack / "projection",
        runner_authenticator=authenticator,
        adapters=adapters,
    )
    control_plane = None
    runner = None
    if authenticator is not None:
        control_plane = InProcessControlPlane(service, runner_id="runner-fixture")
        runner = RunnerClient(
            runner_id="runner-fixture",
            systems=("edms",),
            control_plane=control_plane,
            adapter_factory=_adapter_factory(target, timeouts, clock),
            clock=clock,
        )
        # A runner that has never reported in leaves its adapter `degraded`, so
        # the control plane would refuse to lease. Announce once, as a real
        # runner does on start-up.
        runner.announce(browser_version="synthetic-1")
    return Env(app, TestClient(app), clock, pack, target, control_plane, runner)


def _adapter_factory(target: Any, timeouts: Any, clock: Any) -> Any:
    from fixtures.projection import build_fixture_adapter
    from fixtures.projection.runner import register_definition

    def factory(job: dict[str, Any], *, on_frame: Any, heartbeat: Any) -> Any:
        adapter = build_fixture_adapter(
            job["operation"].split(".", 1)[0],
            target=target,
            timeouts=timeouts,
            clock=clock,
            probe=_probe_factory(target),
            on_frame=on_frame,
            heartbeat=heartbeat,
        )
        register_definition(adapter, job["definition"])
        return adapter

    return factory


class _FixtureVerifier:
    """A pinned TokenVerifier seam. No live Entra tenant is contacted."""

    def verify(self, token: str):
        from review_queue.auth import TokenClaims, TokenVerificationError

        oid = OIDS.get(token)
        if oid is None:
            raise TokenVerificationError("unknown fixture token")
        return TokenClaims(tid=TENANT, oid=oid)


# --- seeding --------------------------------------------------------------------------


def _drain(app, cycles: int = 32) -> None:
    for _ in range(cycles):
        if app.state.dispatcher.dispatch_once() == 0:
            break


def _seed(env: Env) -> str:
    from claim_core import FieldWrite
    from claim_core.schemas import ClaimCreate
    from fixtures.projection import SEED_VALUES

    claim = env.app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), AGENT
    )
    env.app.state.claim_service.write_fields(
        claim.id,
        [
            FieldWrite(
                path=path,
                value=value,
                value_type=value_type,
                source_type="human",
                source_ref={"user_id": OFFICER},
                verification_state="human_verified",
            )
            for path, (value, value_type) in SEED_VALUES.items()
        ],
        OFFICER,
    )
    return claim.id


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
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _projection(env: Env, projection_id: str) -> dict[str, Any]:
    with env.app.state.engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM projections WHERE id = :id"), {"id": projection_id}
            )
            .mappings()
            .first()
        )
    return {key: _loads(value) for key, value in dict(row).items()}


def _request(env: Env, claim_id: str, operation: str = OPERATION):
    return env.app.state.projection_agent.request(
        claim_id=claim_id, operation=operation, actor=AGENT
    )


def _reviews(env: Env, claim_id: str | None = None) -> list[dict[str, Any]]:
    statement = "SELECT id, type, subtype, status, payload FROM review_items"
    parameters: dict[str, Any] = {}
    if claim_id is not None:
        statement += " WHERE claim_id = :claim_id"
        parameters["claim_id"] = claim_id
    with env.app.state.engine.connect() as connection:
        rows = connection.execute(text(statement + " ORDER BY created_at, id"), parameters)
        return [
            {**dict(row._mapping), "payload": _loads(row._mapping["payload"])}
            for row in rows
        ]


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture()
def live_env(tmp_path):
    """The honest production catalogue: nothing is executable and no runner mounts."""

    return _build(tmp_path, "prod", level=None, with_adapter=False)


@pytest.fixture()
def env(tmp_path):
    """The synthetic fixture pack with an installed runner identity and adapter."""

    from fixtures.projection import FixtureRunnerAuthenticator

    return _build(
        tmp_path,
        "fixture",
        pack=_fixture_pack(tmp_path),
        authenticator=FixtureRunnerAuthenticator(),
    )


# --- 1. no migration and no column change ---------------------------------------------


@pytest.mark.schema_isolated
def test_packet_21_ships_no_migration_and_leaves_0015_exact(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    versions = REPO / "platform" / "claim_core" / "alembic" / "versions"
    heads = sorted(path.name for path in versions.glob("*.py"))
    assert not any(name.startswith("0016") for name in heads), heads

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/migration.db")
    config = Config(str(REPO / "platform" / "claim_core" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "platform" / "claim_core" / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("projections")}
    finally:
        engine.dispose()
    assert columns == {
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


# --- 2. adapter, health, and result contracts -----------------------------------------


def test_adapter_health_and_result_contracts_are_closed_and_reject_unknown_keys():
    from projection_agent.adapters import (
        HEALTH_STATUSES,
        OUTCOMES,
        AdapterContractError,
        AdapterHealth,
        OpResult,
    )

    assert HEALTH_STATUSES == ("healthy", "degraded", "unavailable", "circuit_open")
    assert OUTCOMES == (
        "submitted",
        "completed_existing",
        "ui_drift",
        "known_failure",
        "uncertain_write",
    )
    healthy = AdapterHealth.from_mapping(
        {
            "status": "healthy",
            "checked_at": T0.isoformat(),
            "system": "icon",
            "runner_id": "runner-fixture",
            "reason_code": None,
        }
    )
    assert healthy.status == "healthy"
    with pytest.raises(AdapterContractError):
        AdapterHealth.from_mapping({"status": "healthy", "checked_at": T0.isoformat()})
    with pytest.raises(AdapterContractError):
        AdapterHealth.from_mapping(
            {
                "status": "healthy",
                "checked_at": T0.isoformat(),
                "system": "icon",
                "runner_id": None,
                "reason_code": None,
                "target_error": "boom",
            }
        )
    with pytest.raises(AdapterContractError):
        AdapterHealth(
            status="fine", checked_at=T0, system="icon", runner_id=None, reason_code=None
        )
    with pytest.raises(AdapterContractError):
        OpResult(
            outcome="maybe",
            last_completed_step=None,
            write_ids=(),
            readback_keys={},
            evidence_ids=(),
            reason_code=None,
        )
    with pytest.raises(AdapterContractError):
        OpResult.from_mapping({"outcome": "submitted", "stack_trace": "..."})
    uncertain = OpResult(
        outcome="uncertain_write",
        last_completed_step="s15",
        write_ids=("submit_workflow",),
        readback_keys={},
        evidence_ids=(),
        reason_code="unknown_target_response",
    )
    assert uncertain.may_have_written is True


# --- 3. runtime configuration ---------------------------------------------------------


def test_runtime_config_pins_the_exact_launch_values_and_rejects_invalid_boundaries(tmp_path):
    from projection_agent.config import OperationConfigError, load_runtime_config

    runtime = load_runtime_config(MOTOR_PACK / "projection" / "runtime.yaml")
    assert runtime.runner.heartbeat_seconds == 30
    assert runtime.runner.lease_seconds == 120
    assert runtime.runner.reaper_seconds == 60
    assert runtime.runner.max_attempts == 3
    assert runtime.runner.default_step_timeout_seconds == 20
    assert runtime.runner.edms_step_timeout_seconds == 90
    assert runtime.runner.upload_timeout_seconds == 480
    assert runtime.runner.reflection_poll_seconds == 30
    assert runtime.runner.reflection_timeout_seconds == 600
    assert runtime.runner.screenshot_policy == "before_and_after_every_step"
    assert runtime.runner.session_policy == "isolated_context_per_run"
    assert runtime.control_api_auth_status == "blocked_on_inputs"
    assert runtime.control_api_blocked_on == "runner-machine-identity"
    assert runtime.drift_schedule_status == "pending_capture"

    def _write(document: dict[str, Any]) -> pathlib.Path:
        path = tmp_path / f"runtime-{len(list(tmp_path.glob('runtime-*.yaml')))}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    for overrides in (
        {"heartbeat_seconds": 120},  # heartbeat >= lease
        {"lease_seconds": 50},  # lease < twice the heartbeat
        {"reaper_seconds": 300},  # reaper > lease
        {"max_attempts": 5},  # not the AR-1 ceiling
        {"default_step_timeout_seconds": 21},  # above the PRD class ceiling
        {"edms_step_timeout_seconds": 91},
        {"upload_timeout_seconds": 481},
        {"reflection_poll_seconds": 31},
        {"reflection_timeout_seconds": 601},
        {"screenshot_policy": "on_failure_only"},
        {"session_policy": "shared_context"},
        {"heartbeat_seconds": True},  # a boolean is not an integer duration
    ):
        with pytest.raises(OperationConfigError):
            load_runtime_config(_write(_runtime_document(**overrides)))

    missing = _runtime_document()
    missing["runner"].pop("reaper_seconds")
    with pytest.raises(OperationConfigError):
        load_runtime_config(_write(missing))
    extra = _runtime_document()
    extra["runner"]["retry_forever"] = True
    with pytest.raises(OperationConfigError):
        load_runtime_config(_write(extra))
    live_drift = _runtime_document()
    live_drift["drift"]["schedule"] = {"status": "live", "blocked_on": None}
    assert load_runtime_config(_write(live_drift)).drift_schedule_status == "live"


def test_a_live_drift_schedule_without_a_complete_readback_registry_fails_closed(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    drift = yaml.safe_load((MOTOR_PACK / "projection" / "drift.yaml").read_text("utf-8"))
    drift["status"] = "live"
    drift["blocked_on"] = None
    drift["schedule"].update({"day_of_week": "*", "hour": 2, "minute": 0})
    drift["checks"][0].update(
        {
            "status": "live",
            "blocked_on": None,
            "target_readback": None,
            "claim_path": "reserve.total",
        }
    )
    pack = _fixture_pack(tmp_path, name="bad-drift", drift=drift)
    with pytest.raises(OperationConfigError):
        OperationRegistry(pack / "projection")


# --- 4. the production catalogue is unchanged and non-executable -----------------------


def test_production_catalogue_stays_pending_and_registers_no_executable_row(live_env):
    registry = live_env.app.state.projection_agent.operations
    for operation in registry.all():
        assert operation.mode == "paste_assist"
        assert operation.status in {"pending_capture", "blocked_on_inputs"}
        assert operation.click_path is None
        assert operation.blocked_on is not None
        assert not operation.is_live_rpa
    committed = yaml.safe_load(
        (MOTOR_PACK / "projection" / "operations.yaml").read_text(encoding="utf-8")
    )
    assert all(row["click_path_ref"] is None for row in committed["operations"])
    # GP-1 stays closed: no payment operation is registrable as an executable slot.
    for payment in ("icon.payment_voucher", "edms.claim_payment", "edms.payment_workflow"):
        assert registry.get(payment).status == "blocked_on_inputs"
        assert registry.get(payment).owner_prd == "PRD-12"


def test_the_deployment_target_registry_is_fail_closed_and_holds_no_credential():
    from projection_agent.adapters import AdapterUnavailable
    from projection_agent.runner import load_target_registry

    registry = load_target_registry(REPO / "infra" / "rpa_runner" / "targets.yaml")
    for system in ("icon", "edms"):
        record = registry.systems[system]
        assert record.status == "blocked_on_inputs"
        assert record.base_url is None and record.secret_ref is None
        assert record.blocked_on
        with pytest.raises(AdapterUnavailable):
            registry.require(system)


def test_a_live_target_must_name_https_and_a_secrets_manager_reference(tmp_path):
    from projection_agent.runner import load_target_registry
    from projection_agent.runner.target_adapters import TargetRegistryError

    def _write(icon: dict[str, Any]) -> pathlib.Path:
        path = tmp_path / f"targets-{len(list(tmp_path.glob('targets-*.yaml')))}.yaml"
        path.write_text(
            yaml.safe_dump({"version": 1, "systems": {"icon": icon}}, sort_keys=False),
            encoding="utf-8",
        )
        return path

    arn = "arn:aws:secretsmanager:eu-west-1:1:secret:icon"
    for record in (
        # Plain HTTP, a literal secret, and a blocked record carrying an
        # endpoint are all refused.
        {"status": "live", "blocked_on": None, "base_url": "http://i", "secret_ref": arn},
        {"status": "live", "blocked_on": None, "base_url": "https://i", "secret_ref": "hunter2"},
        {"status": "blocked_on_inputs", "blocked_on": "x", "base_url": "https://i",
         "secret_ref": None},
    ):
        with pytest.raises(TargetRegistryError):
            load_target_registry(_write(record))
    good = _write(
        {
            "status": "live",
            "blocked_on": None,
            "base_url": "https://icon.internal",
            "secret_ref": "arn:aws:secretsmanager:eu-west-1:1:secret:icon",
        }
    )
    assert load_target_registry(good).systems["icon"].is_live


# --- 5. activation refusals -----------------------------------------------------------


def test_fixture_rpa_activation_refuses_below_l2_and_without_a_runner_identity(tmp_path):
    from claim_core import ClaimCoreError
    from fixtures.projection import FixtureRunnerAuthenticator

    pack = _fixture_pack(tmp_path, name="under-levelled")
    with pytest.raises(ClaimCoreError) as below:
        _build(
            tmp_path,
            "l1",
            pack=pack,
            authenticator=FixtureRunnerAuthenticator(),
            level="L1",
        )
    assert below.value.code == "PROJECTION_RPA_NOT_ACTIVATABLE"

    with pytest.raises(ClaimCoreError) as unauthenticated:
        _build(tmp_path, "no-auth", pack=_fixture_pack(tmp_path, name="no-auth-pack"))
    assert unauthenticated.value.code == "PROJECTION_RPA_NOT_ACTIVATABLE"

    with pytest.raises(ClaimCoreError) as unbacked:
        _build(
            tmp_path,
            "no-adapter",
            pack=_fixture_pack(tmp_path, name="no-adapter-pack"),
            authenticator=FixtureRunnerAuthenticator(),
            with_adapter=False,
        )
    assert unbacked.value.code == "PROJECTION_RPA_NOT_ACTIVATABLE"


def test_an_incomplete_executable_definition_fails_startup(tmp_path):
    from fixtures.projection import EDMS_CLAIMS_WORKFLOW
    from projection_agent.config import OperationConfigError, OperationRegistry

    for mutate in (
        lambda d: d["steps"][0].pop("effect"),
        lambda d: d["steps"][0].pop("timeout_class"),
        lambda d: d["steps"][-1].update({"write_id": None}),
        lambda d: d["steps"][-1].update({"postcondition": None}),
        lambda d: d.update({"reconciliation": d["reconciliation"][:-1]}),
        lambda d: d.update({"retry_probe": {}}),
        lambda d: d.update({"failure_policy": "screenshot always"}),
        lambda d: d.update({"preconditions": []}),
        lambda d: d["readback"][0].update({"selector": None}),
        lambda d: d["reconciliation"][12].update({"normaliser": {"kind": "string_exact"}}),
    ):
        definition = json.loads(json.dumps(EDMS_CLAIMS_WORKFLOW))
        mutate(definition)
        pack = _fixture_pack(tmp_path, name=f"broken-{id(mutate)}", click_path=definition)
        with pytest.raises(OperationConfigError):
            OperationRegistry(pack / "projection")


def test_a_gp1_owned_operation_can_never_become_an_executable_slot(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    pack = _fixture_pack(tmp_path, name="gp1")
    catalogue_path = pack / "projection" / "operations.yaml"
    catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    for row in catalogue["operations"]:
        if row["id"] == "edms.payment_workflow":
            row.update(
                {
                    "status": "live",
                    "mode": "rpa",
                    "blocked_on": None,
                    "click_path_ref": "edms.claims_workflow@1.0.0.yaml",
                }
            )
    catalogue_path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
    with pytest.raises(OperationConfigError):
        OperationRegistry(pack / "projection")


# --- 6/7. one deferred action crosses AR-2 --------------------------------------------


def test_l2_stages_one_confirmation_and_no_lease_exists_before_approval(tmp_path):
    from fixtures.projection import FixtureRunnerAuthenticator

    env = _build(
        tmp_path,
        "l2",
        pack=_fixture_pack(tmp_path, name="l2-pack"),
        authenticator=FixtureRunnerAuthenticator(),
        level="L2",
    )
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)

    staged = [
        review
        for review in _reviews(env, claim_id)
        if review["type"] == "DRAFT_RELEASE" and review["subtype"] == "projection_rpa"
    ]
    assert len(staged) == 1
    action = staged[0]["payload"]["action"]
    assert set(action["payload"]) == {
        "projection_id",
        "operation",
        "definition_version",
        "snapshot_hash",
    }
    # No target value, encrypted envelope, selector, credential ref, or blob key.
    serialised = json.dumps(staged[0]["payload"])
    for forbidden in ("Grace Wanjiru", "#policyNo", "secret", "lease_token", "POL-21-0001"):
        assert forbidden not in serialised

    row = _projection(env, projection_id)
    assert row["status"] == "queued"
    assert int(row["attempts"] or 0) == 0
    assert env.control_plane.claim(runner_id="runner-fixture", systems=("edms",)) is None
    assert env.target.open_sessions == 0

    review_id = staged[0]["id"]
    response = env.client.post(
        f"/reviews/{review_id}/resolve",
        json={
            "action": "approve",
            "schema_version": "DRAFT_RELEASE_PROJECTION@1",
            "payload": {
                "capability_id": CAPABILITY,
                "projection_id": projection_id,
                "definition_version": "1.0.0",
                "snapshot_hash": row["payload"]["snapshot_hash"],
                "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
            },
        },
        headers=_h(MANAGER),
    )
    assert response.status_code == 200, response.text
    _drain(env.app)
    assert env.runner.run_once() is not None
    assert _projection(env, projection_id)["status"] == "completed"


def test_l2_reject_leaves_a_paste_fallback_and_makes_no_external_call(tmp_path):
    from fixtures.projection import FixtureRunnerAuthenticator

    env = _build(
        tmp_path,
        "l2-reject",
        pack=_fixture_pack(tmp_path, name="l2-reject-pack"),
        authenticator=FixtureRunnerAuthenticator(),
        level="L2",
    )
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    review_id = next(
        review["id"]
        for review in _reviews(env, claim_id)
        if review["subtype"] == "projection_rpa"
    )
    response = env.client.post(
        f"/reviews/{review_id}/resolve",
        json={
            "action": "reject",
            "schema_version": "DRAFT_RELEASE_PROJECTION@1",
            "payload": {
                "capability_id": CAPABILITY,
                "projection_id": projection_id,
                "definition_version": "1.0.0",
                "snapshot_hash": _projection(env, projection_id)["payload"]["snapshot_hash"],
                "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
                "reason": "operate this one by hand",
            },
        },
        headers=_h(MANAGER),
    )
    assert response.status_code == 200, response.text
    _drain(env.app)
    row = _projection(env, projection_id)
    assert row["mode"] == "paste_assist"
    assert row["status"] == "queued"
    assert env.target.open_sessions == 0
    assert env.control_plane.claim(runner_id="runner-fixture", systems=("edms",)) is None


def test_l3_authorises_a_deferred_job_and_reuses_the_deterministic_sampler(env):
    claim_id = _seed(env)
    result = _request(env, claim_id)
    _drain(env.app)
    row = _projection(env, result.projection_id)
    assert row["status"] == "queued"
    assert row["evidence"]["rpa"]["authorisation"]["run_id"]
    with env.app.state.engine.connect() as connection:
        run = (
            connection.execute(
                text("SELECT status, capability_id FROM agent_runs WHERE id = :id"),
                {"id": row["evidence"]["rpa"]["run_id"]},
            )
            .mappings()
            .first()
        )
    assert run["capability_id"] == CAPABILITY
    assert run["status"] == "running"  # the run stays open until reconciliation
    selector = env.app.state.eval_harness.autonomy
    assert hasattr(selector, "emit_sample_review")


def test_only_the_gate_helper_may_call_adapter_execute():
    import tools.ci.banned_calls as guard  # noqa: PLC0415 - CI guard under test

    assert guard.scan_text("result = adapter.execute(op, payload, run_id)\n")
    assert guard.scan_text("adapter.execute_op(payload)\n")
    gate = (REPO / "platform" / "agent_runtime" / "gate.py").read_text(encoding="utf-8")
    assert guard.is_exempt(pathlib.Path("platform/agent_runtime/gate.py"), gate)
    for path in sorted((REPO / "agents" / "projection_agent").rglob("*.py")):
        text_body = path.read_text(encoding="utf-8")
        assert not guard.is_exempt(path, text_body), path
        assert not guard.scan_text(text_body), path


# --- 9/10/11. lease, token, and attempts ----------------------------------------------


def test_the_runner_can_only_pull_authorised_rows_and_holds_one_lease(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    first = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    assert first is not None and first["projection_id"] == projection_id
    assert env.control_plane.claim(runner_id="runner-other", systems=("edms",)) is None
    # A runner that declares only ICON is never handed an EDMS row.
    assert env.control_plane.claim(runner_id="runner-fixture", systems=("icon",)) is None
    row = _projection(env, projection_id)
    assert row["status"] == "executing"
    assert row["attempts"] == 1


def test_the_raw_lease_token_is_never_stored_and_stale_callbacks_fail(env):
    from claim_core import ClaimCoreError

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    token = job["lease_token"]
    row = _projection(env, projection_id)
    lease = row["evidence"]["rpa"]["lease"]
    assert token not in json.dumps(row)
    assert lease["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()

    for bad_token in ("", "deadbeef", token[:-1] + "0"):
        with pytest.raises(ClaimCoreError) as error:
            env.control_plane.heartbeat(
                projection_id, token=bad_token, runner_id="runner-fixture"
            )
        assert error.value.status_code in {404, 409}
    with pytest.raises(ClaimCoreError):
        env.control_plane.heartbeat(projection_id, token=token, runner_id="runner-other")

    beat = env.control_plane.heartbeat(
        projection_id, token=token, runner_id="runner-fixture"
    )
    assert beat["expires_at"] == (env.clock() + timedelta(seconds=120)).isoformat()
    assert _projection(env, projection_id)["attempts"] == 1


def test_attempts_increment_only_on_lease_and_stop_at_three(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    for expected in (1, 2, 3):
        job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
        assert job is not None
        assert _projection(env, projection_id)["attempts"] == expected
        # The lease expires without a result: the reaper safely requeues it.
        env.clock.advance(seconds=200)
        env.app.state.projection_agent.rpa.reap_leases()
    _drain(env.app)
    assert _projection(env, projection_id)["status"] == "failed"
    assert env.control_plane.claim(runner_id="runner-fixture", systems=("edms",)) is None
    subtypes = {
        review["subtype"] for review in _reviews(env, claim_id) if review["type"] == "EXCEPTION"
    }
    assert "projection_attempts_exhausted" in subtypes


# --- 12/13. the 14-field synthetic run -------------------------------------------------


def test_the_fourteen_field_workflow_writes_in_order_with_complete_evidence(env):
    from fixtures.projection import EDMS_FIELD_PATHS
    from fixtures.projection.click_path import FOLDER_REF, SEED_VALUES

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    outcome = env.runner.run_once()
    assert outcome is not None
    _drain(env.app)

    row = _projection(env, projection_id)
    assert row["status"] == "completed"
    assert row["divergence"] is None
    assert row["readback"]["paths"] == ["external.edms.folder_ref"]

    # Exact target order, and money converted exactly at the declared boundary.
    assert [entry for entry in env.target.values] == [
        selector for _s, _p, _e, selector in EDMS_FIELD_PATHS
    ]
    assert env.target.values["#reserveShillings"] == "142656.00"
    assert env.target.values["#excessCents"] == str(SEED_VALUES["policy.excess"][0])
    assert env.target.submitted is True

    frames = row["evidence"]["rpa"]["attempts"][0]["frames"]
    phases: dict[str, set[str]] = {}
    for frame in frames:
        phases.setdefault(frame["step_id"], set()).add(frame["phase"])
    assert len(phases) == 15
    assert all({"before", "after"} <= value for value in phases.values())
    assert [frame["sequence"] for frame in frames] == list(range(1, len(frames) + 1))

    completed = _events(env, "projection.completed", claim_id)
    assert len(completed) == 1
    assert completed[0]["payload"]["readback_paths"] == ["external.edms.folder_ref"]
    current = env.app.state.claim_service.snapshot_current_fields(
        claim_id, ["external.edms.folder_ref"]
    )["external.edms.folder_ref"]
    assert current.value == FOLDER_REF
    assert current.source_type == "projection_readback"


def test_a_completed_run_is_graded_by_gproc_against_its_declared_cop_stages(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    assert _projection(env, projection_id)["status"] == "completed"

    graders = env.app.state.eval_harness.graders
    assert graders.get("G-PROC").status == "live"
    with env.app.state.engine.connect() as connection:
        runs = (
            connection.execute(
                text(
                    "SELECT result, severity, detail FROM grader_runs "
                    "WHERE grader_id = 'G-PROC' AND claim_id = :id"
                ),
                {"id": claim_id},
            )
            .mappings()
            .all()
        )
    assert len(runs) == 1
    assert runs[0]["result"] == "pass"
    assert runs[0]["severity"] == "major"
    detail = _loads(runs[0]["detail"])
    assert detail["declared"] == ["authorise", "lease", "execute", "readback", "reconcile"]
    assert detail["completed"] == detail["declared"]
    mapping = yaml.safe_load(
        (MOTOR_PACK / "eval" / "grader_map.yaml").read_text(encoding="utf-8")
    )
    row = mapping["output_types"]["projection_execution"]
    assert {"id": "G-VAL", "severity": "critical"} in row["graders"]
    assert {"id": "G-PROC", "severity": "major"} in row["graders"]


def test_every_browser_context_is_isolated_and_closed_on_success_and_failure(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    assert env.target.open_sessions == 1
    assert env.target.closed_sessions == 1

    other = _seed(env)
    other_projection = _request(env, other).projection_id
    _drain(env.app)
    env.target.rename("#insuredPhone")
    env.runner.run_once()
    assert env.target.open_sessions == 2
    assert env.target.closed_sessions == 2
    assert _projection(env, other_projection)["status"] == "queued"
    assert projection_id != other_projection


# --- 14/15. selector drift -------------------------------------------------------------


def test_a_changed_selector_halts_opens_the_circuit_and_falls_back_before_a_write(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.rename("#insuredPin")
    env.runner.run_once()
    _drain(env.app)

    row = _projection(env, projection_id)
    assert row["status"] == "queued"
    assert row["mode"] == "paste_assist"
    assert env.target.submitted is False
    attempt = row["evidence"]["rpa"]["attempts"][0]
    assert attempt["outcome"] == "ui_drift"
    assert any(frame["phase"] == "failure" for frame in attempt["frames"])

    drift_reviews = [
        review for review in _reviews(env, claim_id) if review["subtype"] == "ui_drift"
    ]
    assert len(drift_reviews) == 1
    circuit = env.app.state.projection_agent.rpa.circuit(OPERATION)
    assert circuit["status"] == "open"
    assert circuit["definition_version"] == "1.0.0"
    health = env.app.state.projection_agent.rpa.adapter_health("edms")
    assert health.status == "circuit_open"
    # The signed pack file is never edited at runtime.
    committed = (env.pack / "projection" / "operations.yaml").read_text(encoding="utf-8")
    assert "mode: rpa" in committed


def test_a_duplicated_selector_is_drift_and_the_runner_never_picks_one(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.duplicate("#lossDate")
    env.runner.run_once()
    _drain(env.app)
    attempt = _projection(env, projection_id)["evidence"]["rpa"]["attempts"][0]
    assert attempt["outcome"] == "ui_drift"
    assert attempt["reason_code"] == "selector_multiple_matches"
    assert env.target.values.get("#lossDate") is None


def test_selector_failure_after_a_possible_write_is_uncertain_and_offers_no_retry(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    # The write lands, but its captured postcondition never appears.
    env.target.reflect = False
    env.runner.run_once()
    _drain(env.app)

    row = _projection(env, projection_id)
    assert row["status"] == "failed"
    assert row["mode"] == "rpa"  # never offered as a paste fallback
    assert row["completed_at"] is not None
    exception = next(
        review for review in _reviews(env, claim_id) if review["subtype"] == "uncertain_write"
    )
    body = json.dumps(exception["payload"])
    for forbidden in ("retry", "Grace Wanjiru", "#policyNo"):
        assert forbidden not in body
    assert exception["payload"]["projection_id"] == projection_id
    assert env.control_plane.claim(runner_id="runner-fixture", systems=("edms",)) is None


# --- 16/17. the two EDMS known failures ------------------------------------------------


def test_a_duplicate_filename_renames_once_exactly_and_then_fails_visibly(env):
    from projection_agent.runner.browser import TargetKnownFailure

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.raise_signature = "EDMS-DUP-FILENAME"
    env.runner.run_once()
    _drain(env.app)
    row = _projection(env, projection_id)
    attempt = row["evidence"]["rpa"]["attempts"][0]
    assert attempt["outcome"] == "known_failure"
    assert attempt["reason_code"] == "duplicate_filename"
    assert row["status"] == "failed"
    # A signature nobody captured is uncertain, never a guessed known failure.
    assert TargetKnownFailure("s15", "EDMS-UNSEEN").signature == "EDMS-UNSEEN"


def test_an_unmapped_target_signature_is_uncertain_write_not_a_guessed_handler(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.raise_signature = "EDMS-SOMETHING-NEW"
    env.runner.run_once()
    _drain(env.app)
    attempt = _projection(env, projection_id)["evidence"]["rpa"]["attempts"][0]
    assert attempt["outcome"] == "uncertain_write"
    assert attempt["reason_code"] == "unmapped_target_signature"


def test_edms_reflection_polling_is_pack_configured_and_never_resubmits(env):
    runtime = env.app.state.projection_agent.operations.runtime.runner
    assert runtime.reflection_poll_seconds == 30
    assert runtime.reflection_timeout_seconds == 600
    assert runtime.upload_timeout_seconds == 480
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.reflect = False
    env.runner.run_once()
    _drain(env.app)
    assert len(env.target.records) == 1  # submitted exactly once, never resubmitted
    assert _projection(env, projection_id)["status"] == "failed"


# --- 18/19. crash recovery -------------------------------------------------------------


def test_a_runner_killed_before_a_write_safely_re_leases(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    assert job is not None
    env.clock.advance(seconds=200)
    recovered = env.app.state.projection_agent.rpa.reap_leases()
    assert recovered["recovered"][0]["outcome"] == "requeued"
    row = _projection(env, projection_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1  # an unclaimed re-queue does not re-increment
    assert env.runner.run_once() is not None
    assert _projection(env, projection_id)["status"] == "completed"
    assert len(env.target.records) == 1


def test_a_runner_killed_after_submit_probes_first_and_creates_no_duplicate(env):
    from fixtures.projection import EDMS_FIELD_PATHS

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    _upload_full_evidence(env, projection_id, job["lease_token"])
    # Simulate the target having accepted the write before the runner died.
    session = env.target.session()
    for step_id, path, _encoding, selector in EDMS_FIELD_PATHS:
        del step_id, path
        session.fill(selector, _target_value(env, claim_id, selector), timeout_seconds=1)
    session.click("role=button[name='Submit']", timeout_seconds=1)
    session.close()
    _record_last_step(env, projection_id, job, "s15", ["submit_workflow"])
    env.clock.advance(seconds=200)

    recovered = env.app.state.projection_agent.rpa.reap_leases()
    assert recovered["recovered"][0]["outcome"] == "recovered_prior_completion"
    _drain(env.app)
    assert len(env.target.records) == 1
    row = _projection(env, projection_id)
    assert row["status"] in {"completed", "diverged"}
    assert row["status"] == "completed"


def test_an_ambiguous_or_unavailable_probe_is_uncertain_and_executes_nothing(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    env.target.records.extend([{"#policyNo": "POL-21-0001"}, {"#policyNo": "POL-21-0001"}])
    _record_last_step(env, projection_id, job, "s15", ["submit_workflow"])
    env.clock.advance(seconds=200)
    recovered = env.app.state.projection_agent.rpa.reap_leases()
    assert recovered["recovered"][0]["outcome"] == "uncertain_write"
    assert _projection(env, projection_id)["status"] == "failed"
    assert env.target.submitted is False  # no execution happened during recovery


def _target_value(env: Env, claim_id: str, selector: str) -> str:
    from fixtures.projection import EDMS_FIELD_PATHS
    from projection_agent.paste import encode_copy_value

    path, encoding = next(
        (path, encoding)
        for _s, path, encoding, candidate in EDMS_FIELD_PATHS
        if candidate == selector
    )
    snapshot = env.app.state.claim_service.snapshot_current_fields(claim_id, [path])[path]
    plain = env.app.state.claim_service.reveal_snapshot_value(
        claim_id, path=path, value=snapshot.value, actor=RUNNER_ACTOR
    )
    return encode_copy_value(plain, value_type=snapshot.value_type, encoding=encoding)


def _record_last_step(
    env: Env, projection_id: str, job: dict[str, Any], step_id: str, write_ids: list[str]
) -> None:
    """Persist the durable last-completed step the dead runner reached."""

    from projection_agent.models import Projection

    del job
    with env.app.state.projection_agent._guard(projection_id) as session:
        row = session.get(Projection, projection_id)
        coordinator = env.app.state.projection_agent.rpa
        rpa = coordinator.rpa_evidence(row)
        attempt = int(row.attempts or 0)
        record = coordinator._attempt_record(rpa, attempt)
        coordinator._upsert_attempt(
            rpa,
            {
                **record,
                "attempt": attempt,
                "last_completed_step": step_id,
                "write_ids": list(write_ids),
            },
        )
        coordinator.store_rpa_evidence(row, rpa)


# --- 20/21. reconciliation and divergence ---------------------------------------------


def test_a_formatting_change_no_normaliser_declares_diverges_rather_than_guessing(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    token = job["lease_token"]
    _upload_full_evidence(env, projection_id, token)
    inputs = _observed_inputs(env, claim_id)
    inputs["s13"] = "142,656.00"  # a thousands separator nobody captured
    env.control_plane.post_result(
        projection_id,
        token=token,
        runner_id="runner-fixture",
        result={
            "outcome": "submitted",
            "last_completed_step": "s15",
            "write_ids": ["submit_workflow"],
            "readback_keys": {
                "inputs": inputs,
                "outputs": {"folder_ref": "EDMS/2026/004521"},
            },
            "evidence_ids": [],
            "reason_code": None,
        },
    )
    _drain(env.app)
    row = _projection(env, projection_id)
    assert row["status"] == "diverged"
    assert [entry["path"] for entry in row["divergence"]["paths"]] == ["reserve.total"]
    assert row["divergence"]["detected_by"] == "rpa_readback"


def test_an_immediate_mismatch_protects_values_grades_critically_and_never_corrects(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    token = job["lease_token"]
    _upload_full_evidence(env, projection_id, token)
    inputs = _observed_inputs(env, claim_id)
    inputs["s2"] = "Grace Wanjiro"
    env.control_plane.post_result(
        projection_id,
        token=token,
        runner_id="runner-fixture",
        result={
            "outcome": "submitted",
            "last_completed_step": "s15",
            "write_ids": ["submit_workflow"],
            "readback_keys": {
                "inputs": inputs,
                "outputs": {"folder_ref": "EDMS/2026/004521"},
            },
            "evidence_ids": [],
            "reason_code": None,
        },
    )
    _drain(env.app)

    row = _projection(env, projection_id)
    assert row["status"] == "diverged"
    assert row["completed_at"] is not None
    path = row["divergence"]["paths"][0]
    assert path["path"] == "parties.insured.name"
    assert path["expected_sha256"] != path["actual_sha256"]
    # Protected under the claim DEK; never plaintext in the row.
    assert "Grace Wanjiro" not in json.dumps(row["divergence"])

    diverged = _events(env, "projection.diverged", claim_id)
    assert len(diverged) == 1
    body = json.dumps(diverged[0]["payload"])
    assert "Grace Wanjiro" not in body and "Grace Wanjiru" not in body
    exceptions = [
        review for review in _reviews(env, claim_id) if review["subtype"] == "divergence"
    ]
    assert len(exceptions) == 1
    assert exceptions[0]["payload"]["dispositions"] == [
        "target_out_of_band",
        "platform_snapshot_wrong",
        "target_readback_wrong",
        "unresolved",
    ]
    with env.app.state.engine.connect() as connection:
        grades = connection.execute(
            text(
                "SELECT grader_id, result, severity FROM grader_runs "
                "WHERE claim_id = :id AND grader_id = 'G-VAL' ORDER BY occurred_at"
            ),
            {"id": claim_id},
        ).mappings()
        reconciliation = [row for row in grades if row["severity"] == "critical"]
    assert any(row["result"] == "fail" for row in reconciliation)
    # The claim field is untouched: no auto-correction of either side.
    current = env.app.state.claim_service.hydrate_claim(
        claim_id, OFFICER, paths=["parties.insured.name"]
    )[1]["parties.insured.name"]
    assert current.value == "Grace Wanjiru"


def _observed_inputs(env: Env, claim_id: str) -> dict[str, str]:
    from fixtures.projection import EDMS_FIELD_PATHS

    return {
        step_id: _target_value(env, claim_id, selector)
        for step_id, _path, _encoding, selector in EDMS_FIELD_PATHS
    }


def _upload_full_evidence(env: Env, projection_id: str, token: str) -> None:
    from fixtures.projection import EDMS_FIELD_PATHS

    steps = [step_id for step_id, _p, _e, _s in EDMS_FIELD_PATHS] + ["s15"]
    for step_id in steps:
        for phase in ("before", "after"):
            env.control_plane.upload_evidence(
                projection_id,
                step_id,
                token=token,
                runner_id="runner-fixture",
                phase=phase,
                content=f"PNG:{step_id}:{phase}".encode(),
            )


def test_incomplete_evidence_diverges_rather_than_completing(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    env.control_plane.post_result(
        projection_id,
        token=job["lease_token"],
        runner_id="runner-fixture",
        result={
            "outcome": "submitted",
            "last_completed_step": "s15",
            "write_ids": ["submit_workflow"],
            "readback_keys": {
                "inputs": _observed_inputs(env, claim_id),
                "outputs": {"folder_ref": "EDMS/2026/004521"},
            },
            "evidence_ids": [],
            "reason_code": None,
        },
    )
    _drain(env.app)
    row = _projection(env, projection_id)
    assert row["status"] == "diverged"
    assert row["divergence"]["reason_code"] == "evidence_incomplete"


# --- 22. PII containment ---------------------------------------------------------------


def test_pii_is_absent_from_jobs_at_rest_events_reviews_runs_and_health(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)

    secrets = ("Grace Wanjiru", "21000021", "A002100021X", "0102100021", "+254700000021")
    with env.app.state.engine.connect() as connection:
        surfaces = {
            "events": connection.execute(text("SELECT payload FROM events")).scalars().all(),
            "reviews": connection.execute(
                text("SELECT payload FROM review_items")
            ).scalars().all(),
            "runs": connection.execute(text("SELECT steps FROM agent_runs")).scalars().all(),
            "ledger": connection.execute(
                text("SELECT detail FROM audit_ledger")
            ).scalars().all(),
            "state": connection.execute(
                text("SELECT value FROM platform_state")
            ).scalars().all(),
        }
    for name, rows in surfaces.items():
        body = json.dumps([_loads(row) for row in rows])
        for secret in secrets:
            assert secret not in body, (name, secret)
    row = _projection(env, projection_id)
    # The payload snapshot keeps the stored envelope, never plaintext PII.
    assert "Grace Wanjiru" not in json.dumps(row["payload"])
    health = json.dumps(env.app.state.projection_agent.rpa.systems_health())
    for secret in secrets:
        assert secret not in health

    # Every authorised runner decrypt is access-logged.
    decrypts = [
        event
        for event in _events(env, "pii.decrypted", claim_id)
        if event["payload"].get("field_path") == "parties.insured.name"
    ]
    assert decrypts


def test_authorised_evidence_reads_are_access_logged_and_never_expose_a_blob_key(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)

    view = env.client.get(
        f"/console/claims/{claim_id}/projections/{projection_id}/rpa", headers=_h()
    )
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["status"] == "completed"
    assert body["substate"] == "completed"
    assert "projection-evidence/" not in json.dumps(body)
    evidence_id = body["evidence"][0]["evidence_id"]

    read = env.client.get(
        f"/console/claims/{claim_id}/projections/{projection_id}/evidence/{evidence_id}",
        headers=_h(),
    )
    assert read.status_code == 200
    assert read.headers["Cache-Control"] == "private, no-store"
    assert read.headers["X-Content-Type-Options"] == "nosniff"
    assert hashlib.sha256(read.content).hexdigest() == read.headers["X-Evidence-SHA256"]

    logged = [
        event
        for event in _events(env, "pii.decrypted", claim_id)
        if event["payload"].get("resource_type") == "projection_evidence"
    ]
    assert len(logged) == 1
    assert logged[0]["payload"]["evidence_id"] == evidence_id

    other_claim = _seed(env)
    cross = env.client.get(
        f"/console/claims/{other_claim}/projections/{projection_id}/evidence/{evidence_id}",
        headers=_h(),
    )
    assert cross.status_code == 404


# --- 23. sampled paste readback --------------------------------------------------------


def test_sampled_paste_capture_completes_exactly_and_diverges_on_mismatch(tmp_path):
    from fixtures.projection import FixtureRunnerAuthenticator

    env = _build(
        tmp_path,
        "paste-sample",
        pack=_fixture_pack(tmp_path, name="paste-sample-pack", sampling_rate=100),
        authenticator=FixtureRunnerAuthenticator(),
    )
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)

    projection = env.app.state.projection_agent
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE projections SET mode = 'paste_assist' WHERE id = :id"),
            {"id": projection_id},
        )
    projection.sample_paste_readbacks()
    _drain(env.app)
    review = next(
        item for item in _reviews(env, claim_id) if item["type"] == "PASTE_READBACK_CHECK"
    )

    mismatch = env.client.post(
        f"/console/reviews/{review['id']}/paste-readback/capture",
        json={"observed": {"external.edms.folder_ref": "EDMS/2026/999999"}},
        headers=_h(OFFICER),
    )
    assert mismatch.status_code == 200, mismatch.text
    capture = mismatch.json()
    assert capture["mismatch_paths"] == ["external.edms.folder_ref"]
    assert "EDMS/2026/999999" not in json.dumps(_reviews(env, claim_id))

    resolved = env.client.post(
        f"/reviews/{review['id']}/resolve",
        json={
            "action": "edit_approve",
            "schema_version": "PASTE_READBACK_CHECK@2",
            "payload": {
                "capability_id": CAPABILITY,
                "capture_id": capture["capture_id"],
                "diff": {
                    "typed_changes": [
                        {"path": "external.edms.folder_ref", "kind": "text"}
                    ],
                    "prose_change_ratio": 0.0,
                },
            },
        },
        headers=_h(OFFICER),
    )
    assert resolved.status_code == 200, resolved.text
    _drain(env.app)
    row = _projection(env, projection_id)
    assert row["status"] == "diverged"
    assert row["divergence"]["detected_by"] == "paste_sample"
    # An exact repeat creates no second event or review.
    env.app.state.projection_agent.consume_resolution(
        _Resolved(_events(env, "review.resolved", claim_id)[-1]["payload"])
    )
    assert len(_events(env, "projection.diverged", claim_id)) == 1


@dataclass
class _Resolved:
    payload: dict[str, Any]
    type: str = "review.resolved"
    actor: str = OFFICER


def test_approving_a_sample_is_impossible_while_the_server_comparison_mismatches(tmp_path):
    from fixtures.projection import FixtureRunnerAuthenticator

    env = _build(
        tmp_path,
        "sample-approve",
        pack=_fixture_pack(tmp_path, name="sample-approve-pack", sampling_rate=100),
        authenticator=FixtureRunnerAuthenticator(),
    )
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE projections SET mode = 'paste_assist' WHERE id = :id"),
            {"id": projection_id},
        )
    env.app.state.projection_agent.sample_paste_readbacks()
    _drain(env.app)
    review = next(
        item for item in _reviews(env, claim_id) if item["type"] == "PASTE_READBACK_CHECK"
    )
    capture = env.client.post(
        f"/console/reviews/{review['id']}/paste-readback/capture",
        json={"observed": {"external.edms.folder_ref": "EDMS/2026/999999"}},
        headers=_h(OFFICER),
    ).json()
    refused = env.client.post(
        f"/reviews/{review['id']}/resolve",
        json={
            "action": "approve",
            "schema_version": "PASTE_READBACK_CHECK@2",
            "payload": {
                "capability_id": CAPABILITY,
                "capture_id": capture["capture_id"],
                "diff": {"typed_changes": [], "prose_change_ratio": 0.0},
            },
        },
        headers=_h(OFFICER),
    )
    assert refused.status_code == 409
    assert _projection(env, projection_id)["status"] == "completed"


# --- 24. standing drift ----------------------------------------------------------------


def test_production_drift_stays_pending_and_registers_no_beat_entry(live_env):
    from claim_core import celery_app
    from projection_agent.tasks import DRIFT_BEAT_ENTRY

    status = live_env.app.state.projection_agent.drift.status()
    assert status["status"] == "pending_capture"
    assert status["blocked_on"] == "PRD-09-nightly-map-and-time"
    assert status["schedulable"] is False
    assert all(check["status"] == "pending_capture" for check in status["checks"])
    assert {check["id"] for check in status["checks"]} == {
        "icon_reserve_total",
        "icon_claim_status",
    }
    assert DRIFT_BEAT_ENTRY not in (celery_app.conf.beat_schedule or {})
    assert live_env.app.state.projection_agent.drift.run()["status"] == "blocked_on_inputs"


def test_a_fixture_drift_registry_detects_a_target_edit_within_one_cycle(tmp_path):
    from fixtures.projection import FixtureRunnerAuthenticator

    drift = yaml.safe_load((MOTOR_PACK / "projection" / "drift.yaml").read_text("utf-8"))
    drift.update({"status": "live", "blocked_on": None})
    drift["schedule"].update({"day_of_week": "*", "hour": 2, "minute": 0})
    drift["checks"] = [
        {
            "id": "edms_reserve_total",
            "status": "live",
            "blocked_on": None,
            "source_operation": OPERATION,
            "external_ref": "external.edms.folder_ref",
            "claim_path": "reserve.total",
            "target_readback": "#reserveShillings",
        }
    ]
    env = _build(
        tmp_path,
        "drift",
        pack=_fixture_pack(tmp_path, name="drift-pack", drift=drift),
        authenticator=FixtureRunnerAuthenticator(),
    )
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    assert _projection(env, projection_id)["status"] == "completed"

    assert env.app.state.projection_agent.drift.run()["diverged"] == 0
    env.target.values["#reserveShillings"] = "999999.00"  # an out-of-band target edit
    assert env.app.state.projection_agent.drift.run()["diverged"] == 1
    _drain(env.app)
    row = _projection(env, projection_id)
    assert row["status"] == "diverged"
    assert row["divergence"]["detected_by"] == "nightly_drift"
    # Exact repeat creates no duplicate event or review.
    assert env.app.state.projection_agent.drift.run()["diverged"] == 0
    assert len(_events(env, "projection.diverged", claim_id)) == 1


# --- 25/26. metric, ledger, and notification -------------------------------------------


def test_the_divergence_rate_excludes_non_reconciled_rows_and_returns_null_at_zero(env):
    projection = env.app.state.projection_agent
    assert projection.divergence_rate() == {
        "diverged": 0,
        "reconciled": 0,
        "rate_percent": None,
        "basis": "current_projection_status",
    }
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    # A queued row is neither reconciled nor diverged.
    assert projection.divergence_rate()["rate_percent"] is None
    env.runner.run_once()
    _drain(env.app)
    assert projection.divergence_rate() == {
        "diverged": 0,
        "reconciled": 1,
        "rate_percent": 0,
        "basis": "current_projection_status",
    }
    with env.app.state.engine.begin() as connection:
        connection.execute(
            text("UPDATE projections SET status = 'diverged' WHERE id = :id"),
            {"id": projection_id},
        )
    assert projection.divergence_rate()["rate_percent"] == 100
    portfolio = env.client.get("/console/ops/portfolio", headers=_h(MANAGER)).json()
    tiles = {tile["series_id"]: tile for tile in portfolio["tiles"]}
    assert tiles["projection_divergence_rate"]["status"] == "live"
    assert tiles["projection_divergence_rate"]["data"]["diverged"] == 1


def test_projection_diverged_is_ledgered_and_reaches_the_existing_notify_rule(env):
    from claim_core.ledger import ACTION_MAP

    assert ACTION_MAP["projection.diverged"] == "projection.diverged"
    assert ACTION_MAP["projection.failed"] == "projection.failed"
    notify = yaml.safe_load(
        (MOTOR_PACK / "notify" / "notify.yaml").read_text(encoding="utf-8")
    )
    rule = next(row for row in notify["rules"] if row["id"] == "projection_diverged")
    assert rule["event_type"] == "projection.diverged"
    assert rule["audience"] == "assigned_officer"

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    env.app.state.projection_agent.reconcile.diverge(
        projection_id,
        detected_by="paste_sample",
        mismatches=[],
        actor=AGENT,
        reason_code="manual_probe",
    )
    _drain(env.app)
    with env.app.state.engine.connect() as connection:
        actions = connection.execute(
            text("SELECT action FROM audit_ledger WHERE claim_id = :id"), {"id": claim_id}
        ).scalars().all()
    assert "projection.diverged" in actions


# --- 27/28. approval-pack boundary and money -------------------------------------------


def test_approval_pack_items_twelve_and_thirteen_stay_upload_or_pending(env):
    manifest = yaml.safe_load(
        (MOTOR_PACK / "approval_pack" / "manifest.yaml").read_text(encoding="utf-8")
    )
    rows = {row["order"]: row for row in manifest["items"]}
    for order in (12, 13):
        row = rows[order]
        # Screenshots are audit evidence, never approval-pack artifacts (#292):
        # both rows stay officer-upload / pending-integration.
        assert row["selector"] == "projection_or_upload"
        assert set(row["source_kinds"]) == {"projection_readback", "upload"}
        assert "screenshot" not in json.dumps(row)
    del env


def test_every_money_path_stays_integer_cents_through_the_target_boundary(env):
    from projection_agent.reconcile import NormalisationRefused, normalise

    assert normalise("money_cents_exact", "250000") == 250000
    assert normalise("money_shillings_to_cents_exact", "142656.00") == 14265600
    assert normalise("money_shillings_to_cents_exact", "-0.01") == -1
    for refused in ("142,656.00", "142656", "142656.0", "142656.000", "KES 142656.00", ""):
        with pytest.raises(NormalisationRefused):
            normalise("money_shillings_to_cents_exact", refused)
    with pytest.raises(NormalisationRefused):
        normalise("money_cents_exact", "142656.00")
    with pytest.raises(NormalisationRefused):
        normalise("invented_normaliser", "1")

    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    row = _projection(env, projection_id)
    money = [
        entry
        for entry in row["payload"]["fields"]
        if entry["path"] in {"policy.excess", "reserve.total", "settlement.amount"}
    ]
    assert money and all(isinstance(entry["value"], int) for entry in money)


# --- the internal contract is not mounted without an identity --------------------------


def test_without_an_injected_authenticator_the_runner_contract_is_not_mounted(live_env):
    from projection_agent import runner_control_status

    service = live_env.app.state.projection_agent
    assert service.runner_authenticator is None
    status = runner_control_status(service)
    assert status == {"status": "blocked_on_inputs", "blocked_on": "runner-machine-identity"}
    document = live_env.app.openapi()
    assert not any(path.startswith("/internal/") for path in document["paths"])
    response = live_env.client.post(
        "/internal/projection-runner/jobs/claim", json={"systems": ["edms"]}
    )
    assert response.status_code == 404


def test_the_mounted_runner_routes_refuse_user_identity_and_query_tokens(env):
    from fixtures.projection.runner import FIXTURE_HEADER

    claim_id = _seed(env)
    _request(env, claim_id)
    _drain(env.app)
    base = "/internal/projection-runner/jobs/claim"
    assert env.client.post(base, json={"systems": ["edms"]}).status_code == 403
    # The console edge refuses `X-Actor` outright; the route never sees it.
    assert env.client.post(
        base, json={"systems": ["edms"]}, headers={"X-Actor": OFFICER}
    ).status_code in {400, 403}
    assert env.client.post(
        base,
        json={"systems": ["edms"]},
        headers={"Authorization": "Bearer console-token", FIXTURE_HEADER: "fixture-secret"},
    ).status_code == 403
    assert env.client.post(
        f"{base}?token=fixture-secret", json={"systems": ["edms"]},
        headers={FIXTURE_HEADER: "fixture-secret"},
    ).status_code == 403
    accepted = env.client.post(
        base, json={"systems": ["edms"]}, headers={FIXTURE_HEADER: "fixture-secret"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["job"]["projection_id"]
    assert accepted.headers["Cache-Control"] == "private, no-store"


# --- console read surfaces -------------------------------------------------------------


def test_s6_reports_typed_adapter_rows_and_only_admin_may_clear_a_circuit(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.target.rename("#insuredPin")
    env.runner.run_once()
    _drain(env.app)
    assert _projection(env, projection_id)["mode"] == "paste_assist"

    packs = env.client.get("/console/ops/packs", headers=_h(ADMIN)).json()
    rows = {row["system"]: row for row in packs["adapter_health"]}
    assert set(rows) == {"icon", "edms"}
    assert rows["edms"]["status"] == "circuit_open"
    assert rows["edms"]["circuit_operation_ids"] == [OPERATION]
    assert rows["icon"]["status"] == "unavailable"
    assert rows["icon"]["reason_code"] == "pending_capture"
    body = json.dumps(packs["adapter_health"])
    for forbidden in ("#policyNo", "secret_ref", "base_url", "password"):
        assert forbidden not in body

    assert env.client.get("/console/ops/packs", headers=_h(AUDITOR)).status_code == 200
    forbidden = env.client.post(
        f"/console/ops/projection-circuits/{OPERATION}/clear", headers=_h(AUDITOR)
    )
    assert forbidden.status_code == 403
    # An unqualified reset is refused: no strictly newer definition is installed.
    refused = env.client.post(
        f"/console/ops/projection-circuits/{OPERATION}/clear", headers=_h(ADMIN)
    )
    assert refused.status_code == 409


def test_the_rpa_view_never_leaks_a_selector_credential_or_expected_value(env):
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    env.runner.run_once()
    _drain(env.app)
    body = env.client.get(
        f"/console/claims/{claim_id}/projections/{projection_id}/rpa", headers=_h(AUDITOR)
    ).json()
    serialised = json.dumps(body)
    for forbidden in ("#policyNo", "role=button", "Grace Wanjiru", "projection-evidence/"):
        assert forbidden not in serialised
    assert body["circuit"]["status"] == "closed"
    assert body["reconciliation"]["status"] == "reconciled"
    assert body["capability_id"] == CAPABILITY


# --- PostgreSQL tier -------------------------------------------------------------------


@pytest.mark.postgres_required
def test_concurrent_lease_grants_exactly_one_holder(env):
    if env.app.state.engine.dialect.name != "postgresql":
        pytest.skip("real row locks are asserted on the PostgreSQL tier")
    claim_id = _seed(env)
    _request(env, claim_id)
    _drain(env.app)

    def claim(index: int):
        return env.control_plane.claim(runner_id=f"runner-{index}", systems=("edms",))

    with ThreadPoolExecutor(max_workers=4) as pool:
        grants = [grant for grant in pool.map(claim, range(4)) if grant is not None]
    assert len(grants) == 1


@pytest.mark.postgres_required
def test_concurrent_result_callbacks_apply_exactly_once(env):
    if env.app.state.engine.dialect.name != "postgresql":
        pytest.skip("real row locks are asserted on the PostgreSQL tier")
    claim_id = _seed(env)
    projection_id = _request(env, claim_id).projection_id
    _drain(env.app)
    job = env.control_plane.claim(runner_id="runner-fixture", systems=("edms",))
    token = job["lease_token"]
    _upload_full_evidence(env, projection_id, token)
    result = {
        "outcome": "submitted",
        "last_completed_step": "s15",
        "write_ids": ["submit_workflow"],
        "readback_keys": {
            "inputs": _observed_inputs(env, claim_id),
            "outputs": {"folder_ref": "EDMS/2026/004521"},
        },
        "evidence_ids": [],
        "reason_code": None,
    }

    def post(_index: int):
        try:
            return env.control_plane.post_result(
                projection_id, token=token, runner_id="runner-fixture", result=dict(result)
            )
        except Exception as error:  # noqa: BLE001 - a losing writer is expected
            return {"error": type(error).__name__}

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(post, range(3)))
    _drain(env.app)
    assert len(_events(env, "projection.completed", claim_id)) == 1
    assert _projection(env, projection_id)["status"] == "completed"
