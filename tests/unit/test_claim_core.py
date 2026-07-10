"""Unit coverage for the Packet-1 claim substrate engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from claim_core.app import create_app
from claim_core.dictionary import FieldDefinition, value_matches

AGENT = {"X-Actor": "agent:intake"}
USER = {"X-Actor": "user:01JZXY0000000000000000USER"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app("sqlite://"))


def create_claim(client: TestClient) -> str:
    response = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers=AGENT,
    )
    assert response.status_code == 201
    return response.json()["id"]


def write(path: str, value, value_type: str, **overrides) -> dict:
    body = {
        "path": path,
        "value": value,
        "value_type": value_type,
        "source_type": "extraction",
        "verification_state": "extracted",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("definition", "value", "expected"),
    [
        (FieldDefinition("string", "none"), "text", True),
        (FieldDefinition("string", "none"), 3, False),
        (FieldDefinition("money", "none"), 125_00, True),
        (FieldDefinition("money", "none"), True, False),
        (FieldDefinition("date", "none"), "2026-02-28", True),
        (FieldDefinition("date", "none"), "2026-02-30", False),
        (FieldDefinition("date", "none"), 1, False),
        (FieldDefinition("datetime", "none"), "2026-07-10T10:00:00Z", True),
        (FieldDefinition("datetime", "none"), "2026-07-10T10:00:00", False),
        (FieldDefinition("datetime", "none"), "not-a-date", False),
        (FieldDefinition("bool", "none"), False, True),
        (FieldDefinition("object", "none"), {"key": "value"}, True),
        (FieldDefinition("enum", "none", frozenset({"a"})), "a", True),
        (FieldDefinition("enum", "none", frozenset({"a"})), "b", False),
        (FieldDefinition("unknown", "none"), "value", False),
    ],
)
def test_value_matches_registered_types(definition, value, expected):
    assert value_matches(definition, value) is expected


def test_schema_contains_every_binding_table_and_openapi_renders(client: TestClient):
    tables = set(inspect(client.app.state.engine).get_table_names())
    assert tables == {
        "claims",
        "claim_fields",
        "documents",
        "communications",
        "parties",
        "events",
        "event_deliveries",
    }
    assert "/claims/{claim_id}/fields" in client.app.openapi()["paths"]


def test_alembic_upgrade_and_downgrade(tmp_path: Path):
    database = tmp_path / "migration.db"
    config_path = Path(__file__).parents[2] / "platform/claim_core/alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    command.check(config)

    migrated = create_app(f"sqlite:///{database}")
    assert "claim_fields" in inspect(migrated.state.engine).get_table_names()

    command.downgrade(config, "base")
    assert inspect(migrated.state.engine).get_table_names() == ["alembic_version"]


def test_validation_errors_are_machine_readable_and_atomic(client: TestClient):
    claim_id = create_claim(client)
    cases = [
        (
            write("reserve.total", "12500", "money"),
            "VALUE_TYPE_MISMATCH",
        ),
        (
            write("intimation.channel", "fax", "enum"),
            "VALUE_TYPE_MISMATCH",
        ),
        (
            write("policy.number", "MOT/1", "string", source_type="guess"),
            "VALUE_TYPE_MISMATCH",
        ),
        (
            write(
                "policy.number",
                "MOT/1",
                "string",
                verification_state="unverified",
            ),
            "VALUE_TYPE_MISMATCH",
        ),
        (
            write("loss.date", "2026-02-30", "date"),
            "VALUE_TYPE_MISMATCH",
        ),
        (
            write(
                "parties.insured.phone",
                "+254700000000",
                "string",
                pii_class="none",
            ),
            "VALUE_TYPE_MISMATCH",
        ),
    ]
    for bad_write, code in cases:
        response = client.patch(
            f"/claims/{claim_id}/fields", json={"writes": [bad_write]}, headers=AGENT
        )
        assert response.status_code == 422
        assert response.json()["code"] == code

    assert client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"] == {}


def test_human_verified_requires_human_source_and_user_actor(client: TestClient):
    claim_id = create_claim(client)
    human = write(
        "loss.description",
        "checked",
        "string",
        source_type="human",
        verification_state="human_verified",
    )
    response = client.patch(
        f"/claims/{claim_id}/fields", json={"writes": [human]}, headers=AGENT
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALUE_TYPE_MISMATCH"

    with_confidence = {**human, "confidence": 1}
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [with_confidence]},
        headers=USER,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALUE_TYPE_MISMATCH"


def test_human_can_revise_human_value_with_full_provenance(client: TestClient):
    claim_id = create_claim(client)
    first = write(
        "loss.description",
        "first",
        "string",
        source_type="human",
        source_ref={"user_id": USER["X-Actor"][5:]},
        confidence=None,
        verification_state="human_verified",
    )
    second = {**first, "value": "second"}
    for field_write in (first, second):
        response = client.patch(
            f"/claims/{claim_id}/fields",
            json={"writes": [field_write]},
            headers=USER,
        )
        assert response.status_code == 200

    hydrated = client.get(f"/claims/{claim_id}", headers=USER).json()["fields"]
    assert hydrated["loss.description"]["value"] == "second"
    assert hydrated["loss.description"]["version"] == 2
    assert hydrated["loss.description"]["source_ref"] == first["source_ref"]


def test_confidence_is_returned_as_a_json_number(client: TestClient):
    claim_id = create_claim(client)
    extracted = write(
        "policy.number",
        "MOT/CONF",
        "string",
        source_ref={"document_id": "01JZXY00000000000000000DOC"},
        confidence=0.875,
    )
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [extracted]},
        headers=AGENT,
    )
    assert response.status_code == 200
    confidence = client.get(f"/claims/{claim_id}", headers=AGENT).json()["fields"][
        "policy.number"
    ]["confidence"]
    assert confidence == 0.875
    assert isinstance(confidence, float)


def test_invalid_actor_and_unknown_claim_errors(client: TestClient):
    invalid_actor = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers={"X-Actor": "robot"},
    )
    assert invalid_actor.status_code == 422
    assert invalid_actor.json()["code"] == "VALUE_TYPE_MISMATCH"

    missing = client.patch(
        "/claims/01JZXY000000000000000NOPE0/fields",
        json={"writes": [write("policy.number", "MOT/1", "string")]},
        headers=AGENT,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "CLAIM_NOT_FOUND"
