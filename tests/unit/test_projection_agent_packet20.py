"""PACKET-20 unit coverage for the projection loader and paste mechanics.

These are the fail-closed paths the protected acceptance suite proves once at
the API level: click-path defects, binding resolution, encoding, and the
structural fail-closed edge on a persisted row.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from sqlalchemy import text

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"

pytestmark = pytest.mark.usefixtures()


def _click_path(**overrides: Any) -> dict[str, Any]:
    base = {
        "operation": "icon.claim_register",
        "version": "1.0.0",
        "status": "live",
        "screens": [{"id": "one", "label": "One", "order": 1}],
        "steps": [
            {
                "id": "s1",
                "screen": "one",
                "action": "fill",
                "selector": "#policyNo",
                "value": "{policy.number}",
                "external_encoding": "raw",
                "paste_assist": {"label": "Policy number", "copy": True},
            }
        ],
        "readback": [],
        "validators": {},
    }
    base.update(overrides)
    return base


def _write(tmp_path: pathlib.Path, definition: dict[str, Any]) -> pathlib.Path:
    path = tmp_path / "path.yaml"
    path.write_text(yaml.safe_dump(definition, sort_keys=False), encoding="utf-8")
    return path


def _load(tmp_path: pathlib.Path, definition: dict[str, Any], *, version: str = "1.0.0"):
    from projection_agent.config import load_click_path

    return load_click_path(
        _write(tmp_path, definition),
        operation_id=definition["operation"],
        version=version,
    )


# --- click-path loader ----------------------------------------------------------------


def test_a_minimal_live_definition_loads(tmp_path):
    click_path = _load(tmp_path, _click_path())
    assert [screen.id for screen in click_path.screens] == ["one"]
    assert click_path.steps[0].is_copy_row is True
    assert click_path.steps[0].field_path == "policy.number"


@pytest.mark.parametrize(
    ("definition", "fragment"),
    [
        (_click_path(status="pending_capture"), "cannot back a live operation"),
        (_click_path(screens=[]), "non-empty screens"),
        (
            _click_path(
                screens=[
                    {"id": "one", "label": "One", "order": 1},
                    {"id": "one", "label": "Two", "order": 2},
                ]
            ),
            "duplicate screen id",
        ),
        (
            _click_path(
                screens=[
                    {"id": "one", "label": "One", "order": 1},
                    {"id": "two", "label": "Two", "order": 1},
                ]
            ),
            "duplicate screen order",
        ),
        (
            _click_path(
                screens=[
                    {"id": "one", "label": "One", "order": 2},
                    {"id": "two", "label": "Two", "order": 3},
                ],
                steps=[
                    {
                        "id": "s1",
                        "screen": "one",
                        "action": "click",
                        "selector": "#go",
                    }
                ],
            ),
            "contiguous from 1",
        ),
        (_click_path(steps=[]), "non-empty steps"),
    ],
)
def test_structural_click_path_defects_are_rejected(tmp_path, definition, fragment):
    from projection_agent.config import OperationConfigError

    with pytest.raises(OperationConfigError) as error:
        _load(tmp_path, definition)
    assert fragment in str(error.value)


def test_duplicate_step_ids_and_unknown_screens_and_actions_are_rejected(tmp_path):
    from projection_agent.config import OperationConfigError

    step = dict(_click_path()["steps"][0])
    for mutation, fragment in (
        ({"id": "s1"}, "duplicate step id"),
        ({"id": "s2", "screen": "missing"}, "unknown screen"),
        ({"id": "s3", "action": "hover"}, "unknown action"),
        ({"id": "s4", "retries": 2}, "unknown step keys"),
    ):
        definition = _click_path()
        definition["steps"] = [step, {**step, **mutation}]
        with pytest.raises(OperationConfigError) as error:
            _load(tmp_path, definition)
        assert fragment in str(error.value)


def test_a_copy_row_requires_a_label_a_binding_and_a_copyable_action(tmp_path):
    from projection_agent.config import OperationConfigError

    unlabelled = _click_path()
    unlabelled["steps"][0].pop("paste_assist")
    with pytest.raises(OperationConfigError, match="requires a paste label"):
        _load(tmp_path, unlabelled)

    valueless = _click_path()
    valueless["steps"][0].pop("value")
    valueless["steps"][0].pop("external_encoding")
    with pytest.raises(OperationConfigError, match="copy row with no value binding"):
        _load(tmp_path, valueless)

    clicking = _click_path()
    clicking["steps"][0]["action"] = "click"
    with pytest.raises(OperationConfigError, match="cannot be a copy row"):
        _load(tmp_path, clicking)


def test_target_encodings_must_be_declared_for_every_typed_binding(tmp_path):
    from projection_agent.config import OperationConfigError

    for path, encoding, fragment in (
        ("reserve.total", None, "must declare an external_encoding"),
        ("reserve.total", "raw", "is not declared for value_type 'money'"),
        ("loss.date", None, "must declare an external_encoding"),
        ("policy.excess", "iso", "is not declared for value_type 'money'"),
    ):
        definition = _click_path()
        definition["steps"][0]["value"] = f"{{{path}}}"
        if encoding is None:
            definition["steps"][0].pop("external_encoding")
        else:
            definition["steps"][0]["external_encoding"] = encoding
        with pytest.raises(OperationConfigError) as error:
            _load(tmp_path, definition)
        assert fragment in str(error.value)


def test_a_literal_must_be_declared_and_may_not_hide_a_placeholder(tmp_path):
    from projection_agent.config import OperationConfigError

    declared = _click_path()
    declared["steps"][0].update(value="Own Damage Accidental", value_kind="literal")
    assert _load(tmp_path, declared).steps[0].literal == "Own Damage Accidental"

    smuggled = _click_path()
    smuggled["steps"][0].update(value="{policy.number} extra", value_kind="literal")
    with pytest.raises(OperationConfigError, match="must not contain a placeholder"):
        _load(tmp_path, smuggled)

    contradictory = _click_path()
    contradictory["steps"][0]["value_kind"] = "literal"
    with pytest.raises(OperationConfigError, match="contradicts its value"):
        _load(tmp_path, contradictory)

    unknown_kind = _click_path()
    unknown_kind["steps"][0]["value_kind"] = "value_map"
    with pytest.raises(OperationConfigError, match="not a declared binding kind"):
        _load(tmp_path, unknown_kind)


def test_a_rule_binding_requires_an_exact_declared_true_false_map(tmp_path):
    from projection_agent.config import OperationConfigError

    definition = _click_path()
    definition["steps"][0] = {
        "id": "s1",
        "screen": "one",
        "action": "select",
        "selector": "#lossCause",
        "rule": "R-05",
        "rule_values": {"true": "OD Write Off", "false": "Own Damage Accidental"},
        "external_encoding": "raw",
        "paste_assist": {"label": "Loss cause", "copy": True},
    }
    step = _load(tmp_path, definition).steps[0]
    assert step.rule_id == "R-05"
    assert step.rule_values == {"true": "OD Write Off", "false": "Own Damage Accidental"}

    partial = json.loads(json.dumps(definition))
    partial["steps"][0]["rule_values"] = {"true": "OD Write Off"}
    with pytest.raises(OperationConfigError, match="exact true/false strings"):
        _load(tmp_path, partial)

    doubled = json.loads(json.dumps(definition))
    doubled["steps"][0]["value"] = "{policy.number}"
    with pytest.raises(OperationConfigError, match="two value bindings"):
        _load(tmp_path, doubled)


def test_validator_and_readback_declarations_are_closed(tmp_path):
    from projection_agent.config import OperationConfigError

    live = _click_path(
        validators={"icon_claim_no_regex": {"status": "live", "pattern": "^ICON-[0-9]+$"}},
        readback=[
            {
                "capture": "claim_number",
                "label": "ICON claim number",
                "into": "external.icon.claim_no",
                "assert_format": "icon_claim_no_regex",
            }
        ],
    )
    parsed = _load(tmp_path, live)
    assert parsed.readback[0].required is True
    assert parsed.validators["icon_claim_no_regex"].status == "live"

    pending = json.loads(json.dumps(live))
    pending["validators"]["icon_claim_no_regex"] = {
        "status": "pending_capture",
        "blocked_on": "open-item-3",
    }
    assert _load(tmp_path, pending).validators["icon_claim_no_regex"].pattern is None

    for mutation, fragment in (
        ({"status": "live"}, "requires a pattern"),
        ({"status": "live", "pattern": "["}, "pattern is invalid"),
        ({"status": "pending_capture"}, "requires a blocker"),
        ({"status": "guessed", "pattern": "^a$"}, "status 'guessed' is invalid"),
        (
            {"status": "live", "pattern": "^a$", "blocked_on": "x"},
            "must not carry a blocker",
        ),
    ):
        broken = json.loads(json.dumps(live))
        broken["validators"]["icon_claim_no_regex"] = mutation
        with pytest.raises(OperationConfigError) as error:
            _load(tmp_path, broken)
        assert fragment in str(error.value)

    undeclared = json.loads(json.dumps(live))
    undeclared["validators"] = {}
    with pytest.raises(OperationConfigError, match="undeclared validator"):
        _load(tmp_path, undeclared)

    duplicated = json.loads(json.dumps(live))
    duplicated["readback"].append(dict(duplicated["readback"][0]))
    with pytest.raises(OperationConfigError, match="duplicate readback target"):
        _load(tmp_path, duplicated)


def test_click_path_operation_and_key_mismatches_are_rejected(tmp_path):
    from projection_agent.config import OperationConfigError

    unknown = _click_path()
    unknown["timeout_seconds"] = 20
    with pytest.raises(OperationConfigError, match="unknown click path keys"):
        _load(tmp_path, unknown)


# --- encoding -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "value_type", "encoding", "expected"),
    [
        (14_265_600, "money", "shillings", "142656.00"),
        (1_500_50, "money", "shillings", "1500.50"),
        (5, "money", "shillings", "0.05"),
        (-1_00, "money", "shillings", "-1.00"),
        (14_265_600, "money", "cents", "14265600"),
        ("POL-1", "string", "raw", "POL-1"),
        ("2026-07-01", "date", "iso", "2026-07-01"),
    ],
)
def test_encode_copy_value_is_exact(value, value_type, encoding, expected):
    from projection_agent.paste import encode_copy_value

    assert encode_copy_value(value, value_type=value_type, encoding=encoding) == expected


def test_encode_copy_value_refuses_an_uncaptured_shape():
    from projection_agent.paste import SnapshotBlocked, encode_copy_value

    for value, encoding in ((True, "cents"), ("x", "shillings"), (5, "raw"), ("", "iso")):
        with pytest.raises(SnapshotBlocked):
            encode_copy_value(value, value_type="money", encoding=encoding)
    with pytest.raises(SnapshotBlocked):
        encode_copy_value("x", value_type="string", encoding="invented")


def test_datetime_values_encode_as_iso():
    from projection_agent.paste import encode_copy_value

    moment = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    assert encode_copy_value(moment, value_type="datetime", encoding="iso").startswith(
        "2026-07-01T09:30"
    )


# --- registry contains / capability ids -----------------------------------------------


def test_the_production_registry_exposes_ids_and_membership():
    from projection_agent.config import OPERATION_IDS, OperationRegistry

    registry = OperationRegistry(MOTOR_PACK / "projection")
    assert "icon.claim_register" in registry
    assert "icon.not_real" not in registry
    assert len(registry.all()) == len(OPERATION_IDS)
    assert registry.get("icon.claim_register").capability_id == "project.icon.claim_register"


def test_an_unknown_operation_lookup_raises():
    from projection_agent.config import OperationConfigError, OperationRegistry

    registry = OperationRegistry(MOTOR_PACK / "projection")
    with pytest.raises(OperationConfigError, match="unknown operation"):
        registry.get("icon.not_real")


def test_a_missing_catalogue_file_fails_closed(tmp_path):
    from projection_agent.config import OperationConfigError, OperationRegistry

    with pytest.raises(OperationConfigError, match="does not exist"):
        OperationRegistry(tmp_path / "absent")


def test_the_grader_map_must_cover_projection_readback(tmp_path):
    import shutil

    from projection_agent import _assert_grader_coverage
    from projection_agent.config import OperationConfigError, OperationRegistry

    pack = tmp_path / "motor"
    shutil.copytree(MOTOR_PACK, pack)
    registry = OperationRegistry(pack / "projection")
    _assert_grader_coverage(pack, registry)

    mapping_path = pack / "eval" / "grader_map.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mapping["output_types"].pop("projection_readback")
    mapping_path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    with pytest.raises(OperationConfigError, match="does not register projection_readback"):
        _assert_grader_coverage(pack, registry)


# --- structural fail-closed edge -------------------------------------------------------


def _minimal_app(tmp_path):
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    url = os.environ.get("DATABASE_URL", f"sqlite:///{tmp_path}/unit20.db")
    app = create_app(url)
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    build_eval_harness(app)
    build_review_queue(app, roles={})
    return app


def _rule_pack(tmp_path: pathlib.Path) -> pathlib.Path:
    """A fixture whose live registration step is bound to a rule outcome."""

    import shutil

    pack = tmp_path / "rule-pack" / "motor"
    pack.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MOTOR_PACK, pack)
    definition = _click_path(
        version="1.1.0",
        steps=[
            {
                "id": "s1",
                "screen": "one",
                "action": "select",
                "selector": "#lossCause",
                "rule": "R-05",
                "rule_values": {
                    "true": "OD Write Off",
                    "false": "Own Damage Accidental",
                },
                "external_encoding": "raw",
                "paste_assist": {"label": "Loss cause", "copy": True},
            },
            {
                "id": "s2",
                "screen": "one",
                "action": "fill",
                "selector": "#module",
                "value": "Claims",
                "value_kind": "literal",
                "external_encoding": "raw",
                "paste_assist": {"label": "Module", "copy": True},
            },
        ],
    )
    (pack / "projection" / "icon.claim_register@1.1.0.yaml").write_text(
        yaml.safe_dump(definition, sort_keys=False), encoding="utf-8"
    )
    catalogue_path = pack / "projection" / "operations.yaml"
    catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    catalogue["operations"] = [
        {
            **entry,
            "version": "1.1.0",
            "status": "live",
            "blocked_on": None,
            "click_path_ref": "icon.claim_register@1.1.0.yaml",
        }
        if entry["id"] == "icon.claim_register"
        else entry
        for entry in catalogue["operations"]
    ]
    catalogue_path.write_text(yaml.safe_dump(catalogue, sort_keys=False), encoding="utf-8")
    return pack


def _rule_run(app, claim_id: str, *, run_id: str, fired: bool | None, at: datetime) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO rule_runs (id, claim_id, rule_id, rule_version, pack_id, "
                "pack_version, status, fired, outcome, inputs_snapshot, missing_inputs, "
                "actor, evaluated_at) VALUES (:id, :claim_id, 'R-05', '2.0.0', 'motor', "
                "'motor@1.0.0', 'evaluated', :fired, NULL, :inputs, :missing, :actor, :at)"
            ),
            {
                "id": run_id,
                "claim_id": claim_id,
                "fired": fired,
                "inputs": json.dumps({}),
                "missing": json.dumps([]),
                "actor": "agent:cop",
                "at": at,
            },
        )


def test_a_rule_binding_resolves_to_one_completed_run_or_blocks(tmp_path):
    from claim_core.schemas import ClaimCreate
    from projection_agent.config import OperationRegistry
    from projection_agent.models import Projection
    from projection_agent.service import ProjectionService

    app = _minimal_app(tmp_path)
    from claim_core import Base

    Base.metadata.create_all(app.state.engine, tables=[Projection.__table__])
    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), "agent:projection"
    )
    service = ProjectionService(
        app, OperationRegistry(_rule_pack(tmp_path) / "projection")
    )

    missing = service.request(
        claim_id=claim.id, operation="icon.claim_register", actor="agent:projection"
    )
    assert missing.status == "blocked_on_inputs"
    assert missing.blocked_on == "rule_run_missing:R-05"

    at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    _rule_run(app, claim.id, run_id="01HP20RULEA000000000AAAAA", fired=None, at=at)
    indeterminate = service.request(
        claim_id=claim.id, operation="icon.claim_register", actor="agent:projection"
    )
    assert indeterminate.blocked_on == "rule_run_indeterminate:R-05"

    _rule_run(app, claim.id, run_id="01HP20RULEB000000000AAAAA", fired=True, at=at)
    ambiguous = service.request(
        claim_id=claim.id, operation="icon.claim_register", actor="agent:projection"
    )
    assert ambiguous.blocked_on == "rule_run_ambiguous:R-05"

    later = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    _rule_run(app, claim.id, run_id="01HP20RULEC000000000AAAAA", fired=True, at=later)
    created = service.request(
        claim_id=claim.id, operation="icon.claim_register", actor="agent:projection"
    )
    assert created.status == "created"
    with app.state.engine.connect() as connection:
        payload = connection.execute(
            text("SELECT payload FROM projections WHERE id = :id"),
            {"id": created.projection_id},
        ).scalar()
    fields = json.loads(payload)["fields"] if isinstance(payload, str) else payload["fields"]
    rule_entry = next(entry for entry in fields if entry["path"] == "rule:R-05")
    assert rule_entry["value"] == "OD Write Off"
    assert rule_entry["field_id"] == "01HP20RULEC000000000AAAAA"
    assert rule_entry["version"] == "2.0.0"
    literal_entry = next(entry for entry in fields if entry["path"] == "literal:s2")
    assert literal_entry["value"] == "Claims"
    assert literal_entry["field_id"] is None


def test_a_structurally_invalid_persisted_row_fails_closed_with_an_event(tmp_path):
    from claim_core import ClaimCoreError, new_ulid
    from claim_core.schemas import ClaimCreate
    from projection_agent.config import OperationRegistry
    from projection_agent.models import Projection
    from projection_agent.service import ProjectionService

    app = _minimal_app(tmp_path)
    from claim_core import Base

    Base.metadata.create_all(app.state.engine, tables=[Projection.__table__])
    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), "agent:projection"
    )
    service = ProjectionService(app, OperationRegistry(MOTOR_PACK / "projection"))
    projection_id = new_ulid()
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projections (id, claim_id, operation, mode, status, payload, "
                "attempts, idempotency_key, created_at) VALUES "
                "(:id, :claim_id, 'icon.claim_register', 'paste_assist', 'queued', "
                ":payload, 0, :key, :created_at)"
            ),
            {
                "id": projection_id,
                "claim_id": claim.id,
                # No schema_version, no operation_definition: structurally invalid.
                "payload": json.dumps({"fields": []}),
                "key": f"{claim.id}:icon.claim_register:deadbeef",
                "created_at": datetime.now(UTC),
            },
        )
    with pytest.raises(ClaimCoreError) as error:
        service.paste_view(claim.id, projection_id, actor="user:missing")
    assert error.value.code in {"FORBIDDEN_ROLE", "PROJECTION_FAILED"}

    # The role check runs first; grant one and the structural edge fires.
    app.state.review_queue.service.authorizer.roles = {
        "user:01HP20UNITOFFICER00000AAAA": "claims_officer"
    }
    with pytest.raises(ClaimCoreError) as error:
        service.paste_view(claim.id, projection_id, actor="user:01HP20UNITOFFICER00000AAAA")
    assert error.value.code == "PROJECTION_FAILED"
    with app.state.engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM projections WHERE id = :id"), {"id": projection_id}
        ).scalar()
        failures = connection.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'projection.failed'")
        ).scalar()
    assert status == "failed"
    assert failures == 1


def test_a_pending_definition_is_not_called_a_runtime_failure(tmp_path):
    from claim_core import ClaimCoreError, new_ulid
    from claim_core.schemas import ClaimCreate
    from projection_agent.config import OperationRegistry
    from projection_agent.models import Projection
    from projection_agent.service import ProjectionService

    app = _minimal_app(tmp_path)
    from claim_core import Base

    Base.metadata.create_all(app.state.engine, tables=[Projection.__table__])
    claim = app.state.claim_service.create_claim(
        ClaimCreate(lob="motor", pack_version="motor@1.0.0"), "agent:projection"
    )
    app.state.review_queue.service.authorizer.roles = {
        "user:01HP20UNITOFFICER00000AAAA": "claims_officer"
    }
    service = ProjectionService(app, OperationRegistry(MOTOR_PACK / "projection"))
    projection_id = new_ulid()
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projections (id, claim_id, operation, mode, status, payload, "
                "attempts, idempotency_key, created_at) VALUES "
                "(:id, :claim_id, 'icon.claim_register', 'paste_assist', 'queued', "
                ":payload, 0, :key, :created_at)"
            ),
            {
                "id": projection_id,
                "claim_id": claim.id,
                "payload": json.dumps(
                    {
                        "schema_version": 1,
                        "operation_definition": {
                            "operation": "icon.claim_register",
                            "version": "1.0.0",
                        },
                        "fields": [],
                        "snapshot_hash": "0" * 64,
                    }
                ),
                "key": f"{claim.id}:icon.claim_register:{'0' * 64}",
                "created_at": datetime.now(UTC),
            },
        )
    with pytest.raises(ClaimCoreError) as error:
        service.paste_view(claim.id, projection_id, actor="user:01HP20UNITOFFICER00000AAAA")
    assert error.value.code == "PROJECTION_DEFINITION_UNAVAILABLE"
    with app.state.engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM projections WHERE id = :id"), {"id": projection_id}
        ).scalar()
        failures = connection.execute(
            text("SELECT COUNT(*) FROM events WHERE type = 'projection.failed'")
        ).scalar()
    assert status == "queued"
    assert failures == 0
