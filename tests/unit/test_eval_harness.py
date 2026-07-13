"""Focused Packet-08 unit coverage beyond the protected acceptance contract."""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import attributes

from claim_core.app import create_app
from cop_runtime import build_cop_runtime
from eval_harness import build_eval_harness
from eval_harness.autonomy import is_material_edit
from eval_harness.gating import grade_output
from eval_harness.models import Capability
from eval_harness.policies import load_policies

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"


@pytest.fixture()
def eval_app(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/eval-unit.db")
    build_cop_runtime(app, pack_paths=[MOTOR_PACK])
    return app, build_eval_harness(app)


def test_material_edit_boundary_and_invalid_policy_shapes(tmp_path):
    assert is_material_edit({"typed_changes": [{"kind": "date"}]})
    assert is_material_edit({"prose_change_ratio": 0.151})
    assert not is_material_edit({"prose_change_ratio": 0.15})
    assert not is_material_edit(None)

    invalid_bodies = [
        "[]\n",
        "capabilities: nope\n",
        "capabilities:\n  - {id: x, max_level: L9}\n",
        "capabilities:\n  - {id: x, max_level: L1, initial_level: L2}\n",
        "capabilities:\n  - {id: x, max_level: L1, policy: {unknown: 1}}\n",
        "default_policy: {sampling_rate: 4, sampling_floor: 5}\ncapabilities: []\n",
    ]
    for index, body in enumerate(invalid_bodies):
        path = tmp_path / f"invalid-{index}.yaml"
        path.write_text(body)
        with pytest.raises(ValueError):
            load_policies(path)


def test_pending_grader_is_visible_error_and_gate_seam_does_not_block(eval_app):
    app, harness = eval_app
    decision = grade_output(
        harness,
        "G-CITE",
        {"claim_id": None, "path": "vehicle.reg"},
        actor="agent:eval",
    )
    assert decision.grade.result == "error"
    assert decision.blocked is False
    assert app.state.dispatcher.consumer_names >= {"eval", "autonomy"}
    with pytest.raises(LookupError):
        harness.graders.get("G-MISSING")


def test_sample_review_helper_uses_existing_type_and_l3_only(eval_app):
    app, harness = eval_app
    with harness.sessions.begin() as session:
        capability = session.get(Capability, "intake.acknowledge")
        capability.current_level = "L3"
        policy = dict(capability.policy)
        policy["sampling_rate"] = 100
        capability.policy = policy
        attributes.flag_modified(capability, "policy")

    assert harness.autonomy.emit_sample_review(
        "intake.acknowledge",
        "always-selected",
        claim_id=None,
        underlying_type="DRAFT_RELEASE",
        actor="agent:eval",
    )
    harness.grade(
        "G-CITE",
        {"path": "vehicle.reg", "test_case_id": "pending-slot-test"},
        actor="agent:eval",
    )
    client = TestClient(app)
    response = client.get(
        "/eval/runs?grader=G-CITE",
        headers={"X-Actor": "agent:eval"},
    )
    assert response.status_code == 200
    assert response.json()["runs"][0]["grader_id"] == "G-CITE"

    with harness.sessions.begin() as session:
        session.get(Capability, "intake.acknowledge").current_level = "L2"
    assert not harness.autonomy.emit_sample_review(
        "intake.acknowledge",
        "always-selected",
        claim_id=None,
        underlying_type="DRAFT_RELEASE",
        actor="agent:eval",
    )
