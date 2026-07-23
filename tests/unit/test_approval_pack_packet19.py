"""PACKET-19 unit regressions for the decisions that carry a register entry.

These cover the narrow, fail-closed behaviours the acceptance suite exercises
end to end: the routing contract read, side-effect parsing (#256), the canonical
body hash (#246), the subtype band override (#248), and the pack field-set slot
(#252).
"""
from __future__ import annotations

import json
import pathlib
import shutil
from types import SimpleNamespace

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"


# --- canonical body hash (#246) -----------------------------------------------------


def test_canonical_body_hash_excludes_its_own_field_and_is_order_stable():
    from approval_pack_agent.note import canonical_body_hash

    body = {"template_id": "T-01", "sections": [{"template_slot": "computed"}]}
    digest = canonical_body_hash(body)
    assert canonical_body_hash({**body, "body_sha256": digest}) == digest
    # Key order is not part of the material; content is.
    reordered = {"sections": body["sections"], "template_id": "T-01"}
    assert canonical_body_hash(reordered) == digest
    assert canonical_body_hash({**body, "signable": True}) != digest


# --- side-effect parsing (#256) -----------------------------------------------------


class _Signing:
    """A SigningService bound to nothing but its own parsing rules."""

    def __init__(self):
        from approval_pack_agent.signing import SigningService

        self.service = SigningService.__new__(SigningService)

    def templates(self, side_effects):
        return type(self.service).side_effect_templates(
            self.service, {"side_effects": side_effects}
        )


def test_only_the_captured_render_side_effect_form_is_accepted():
    from claim_core import ClaimCoreError

    signing = _Signing()
    assert signing.templates(["render T-03"]) == ["T-03"]
    assert signing.templates([]) == []
    for unknown in ("notify head_of_claims", "render", "T-03", "render T-03 and T-13"):
        with pytest.raises(ClaimCoreError) as caught:
            signing.templates([unknown])
        assert caught.value.code == "ROUTING_BLOCKED_ON_INPUTS"
        assert caught.value.extra["blocked_on"] == "authority_matrix.side_effects"


# --- exact-role band authority (#248) -----------------------------------------------


def test_exact_role_authorisation_refuses_a_wider_band_and_a_missing_snapshot():
    from review_queue.rbac import Authorizer

    officer = "user:01HP19UNITOFFICER0000AAAAA"
    chairman = "user:01HP19UNITCHAIRMAN000AAAAA"
    authorizer = Authorizer(
        {officer: "claims_manager", chairman: "chairman"},
        {"claims_manager": 700_000_00, "chairman": None},
    )
    assert authorizer.resolve_exact_role_code(
        actor=officer, required_role="claims_manager"
    ) is None
    # The unlimited band does not silently take another role's approval.
    assert authorizer.resolve_exact_role_code(
        actor=chairman, required_role="claims_manager"
    ) == "FORBIDDEN_BAND"
    assert authorizer.resolve_exact_role_code(
        actor="user:01HP19UNITUNKNOWN0000AAAAA", required_role="claims_manager"
    ) == "FORBIDDEN_ROLE"
    for absent in (None, "", 7):
        assert authorizer.resolve_exact_role_code(
            actor=officer, required_role=absent
        ) == "RESOLUTION_BLOCKED_ON_INPUTS"


def test_the_approval_pack_subtype_overrides_the_wrong_band_field_path():
    from review_queue.contracts import ContractRegistry

    registry = ContractRegistry(MOTOR_PACK / "review")
    parent = registry.get("PACK_REVIEW")
    subtype = registry.get("PACK_REVIEW", "approval_pack")
    assert parent.band_amount_path == "assessment.agreed_quote"
    assert parent.band_role_path is None
    # #248: the approval pack is authorised against its immutable snapshot.
    assert subtype.band_amount_path is None
    assert subtype.band_role_path == "required_role"
    assert subtype.resolution_schema == "PACK_REVIEW@2"
    # An unrelated subtype still inherits the parent contract unchanged.
    coverage = registry.get("FIELD_VERIFY", "coverage_manual")
    assert coverage.band_amount_path == registry.get("FIELD_VERIFY").band_amount_path


def test_note_review_at_1_is_retained_for_historical_replay():
    from review_queue.contracts import ContractRegistry

    registry = ContractRegistry(MOTOR_PACK / "review")
    assert registry.get("NOTE_REVIEW").resolution_schema == "NOTE_REVIEW@1"
    assert registry.get("NOTE_REVIEW", "approval_note").resolution_schema == "NOTE_REVIEW@2"
    schema = registry.get("NOTE_REVIEW", "approval_note").schema
    assert set(schema["required"]) == {"capability_id", "draft_id", "body_sha256", "diff"}
    assert schema["properties"]["body_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert schema["additionalProperties"] is False


def test_pack_review_at_2_binds_the_route_snapshot_and_the_fsm_reason_shape():
    schema = json.loads(
        (MOTOR_PACK / "review" / "schemas" / "PACK_REVIEW@2.json").read_text("utf-8")
    )
    assert set(schema["required"]) == {
        "capability_id", "merged_event_id", "note_signed_event_id", "draft_id",
        "body_sha256", "routing_amount_cents", "required_role", "diff",
    }
    assert schema["properties"]["routing_amount_cents"]["type"] == "integer"
    reason = schema["$defs"]["rejection_reason"]
    # #258: the FSM already binds `{code, detail}`; no reason enum is invented.
    assert set(reason["required"]) == {"code", "detail"}
    assert set(reason["properties"]) == {"code", "detail", "field_path"}
    assert "enum" not in reason["properties"]["code"]


# --- pack configuration (#252) ------------------------------------------------------


def _load(pack: pathlib.Path):
    from approval_pack_agent.config import load_config

    return load_config(pack)


def _copy(tmp_path: pathlib.Path) -> pathlib.Path:
    pack = tmp_path / "motor"
    shutil.copytree(MOTOR_PACK, pack)
    return pack


def test_the_icon_note_entry_slot_loads_blocked_with_no_field_order():
    config = _load(MOTOR_PACK)
    field_set = config.field_set("icon.note_entry")
    assert field_set["status"] == "pending_capture"
    assert field_set["blocked_on"] == "open-item-3"
    assert field_set["fields"] == []
    with pytest.raises(LookupError):
        config.field_set("icon.reserve_adjust")


def test_an_uncaptured_field_set_carrying_fields_refuses_to_install(tmp_path):
    from approval_pack_agent.config import PackConfigError

    pack = _copy(tmp_path)
    path = pack / "approval_pack" / "icon.yaml"
    payload = yaml.safe_load(path.read_text("utf-8"))
    payload["field_sets"]["icon.note_entry"]["fields"] = ["note_text"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError, match="empty field list"):
        _load(pack)


def test_a_missing_autosave_interval_refuses_to_install(tmp_path):
    from approval_pack_agent.config import PackConfigError

    pack = _copy(tmp_path)
    path = pack / "approval_pack" / "note.yaml"
    payload = yaml.safe_load(path.read_text("utf-8"))
    del payload["autosave_seconds"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PackConfigError, match="slot map is incomplete"):
        _load(pack)
    assert _load(MOTOR_PACK).autosave_seconds == 5


# --- routing contract ---------------------------------------------------------------


def test_the_routing_contract_reports_the_blocked_calc_and_its_binding_fallback():
    from cop_runtime.pack_loader import load_pack
    from cop_runtime.runtime import CopRuntime

    runtime = CopRuntime.__new__(CopRuntime)
    loaded = load_pack(MOTOR_PACK)
    runtime._packs = {("motor", "1.0.0"): loaded}
    contract = runtime.routing_contract("motor", "1.0.0")
    # PRD-02 §2.5: payable when live, the reserve fallback while it is not.
    assert contract["calc_id"] == "C-08"
    assert contract["calc_status"] == "blocked_on_inputs"
    assert contract["fallback_path"] == "reserve.total"


def test_the_authority_matrix_boundary_is_inclusive_and_the_tail_carries_t03():
    from cop_runtime.routing import load_authority_matrix

    matrix = load_authority_matrix(MOTOR_PACK / "routing" / "authority_matrix.yaml")
    assert matrix.route(4_000_000_00).role == "md"
    assert matrix.route(4_000_000_00).side_effects == []
    over = matrix.route(4_000_000_01)
    assert over.role == "chairman"
    assert over.side_effects == ["render T-03"]
    assert matrix.route(100_000_00).role == "asst_claims_manager"
    assert matrix.route(100_000_01).role == "claims_manager"


# --- ledger and artifact allowlist --------------------------------------------------


def test_every_packet_19_event_maps_through_the_single_ledger_writer():
    from claim_core.ledger import ACTION_MAP

    for event_type in (
        "pack.note_autosaved",
        "pack.note_review_rejected",
        "pack.note_sign_prepared",
        "pack.note_signed",
        "pack.routed",
    ):
        assert ACTION_MAP[event_type] == event_type
    # No duplicate approval/rejection event type was added.
    assert ACTION_MAP["review.resolved"] == "review.resolved"
    assert not any(key.startswith("pack.approved") for key in ACTION_MAP)


def test_only_two_artifact_index_event_types_cross_to_a_browser():
    from approval_pack_agent.workspace import ARTIFACT_EVENT_TYPES

    assert ARTIFACT_EVENT_TYPES == {
        "pack.merged": "blob_key",
        "pack.note_signed": "artifact_blob_key",
    }


def test_the_workspace_resolves_the_highest_lineage_version_not_the_review_id():
    from approval_pack_agent.workspace import NoteWorkspace

    workspace = NoteWorkspace.__new__(NoteWorkspace)
    root = {"id": "D1", "version": 1, "body": {}}
    child = {
        "id": "D2",
        "version": 2,
        "body": {"lineage": {"root_draft_id": "D1", "parent_draft_id": "D1"}},
    }
    other = {"id": "D9", "version": 9, "body": {"lineage": {"root_draft_id": "OTHER"}}}
    assert NoteWorkspace.root_of(root) == "D1"
    assert NoteWorkspace.root_of(child) == "D1"
    workspace.drafts = lambda claim_id: [root, child, other]  # type: ignore[method-assign]
    assert [row["id"] for row in workspace.lineage("C1", "D1")] == ["D1", "D2"]
    assert workspace.current("C1", "D1")["id"] == "D2"


def test_sign_state_reports_a_durable_resolution_rather_than_losing_it():
    from approval_pack_agent.workspace import NoteWorkspace

    workspace = NoteWorkspace.__new__(NoteWorkspace)
    events: dict[str, list[dict]] = {
        "pack.note_sign_prepared": [],
        "pack.note_signed": [],
    }
    workspace.service = SimpleNamespace(_events=lambda claim, type_: events[type_])
    assert workspace.sign_state("C1", "D1") == "unsigned"
    events["pack.note_sign_prepared"].append({"id": "E1", "payload": {"note_draft_id": "D1"}})
    assert workspace.sign_state("C1", "D1") == "signing_pending"
    events["pack.note_signed"].append({"id": "E2", "payload": {"note_draft_id": "D1"}})
    assert workspace.sign_state("C1", "D1") == "signed"
    # Another draft's evidence never resolves this one.
    assert workspace.sign_state("C1", "D2") == "unsigned"
