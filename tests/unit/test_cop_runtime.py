"""Focused unit and fail-closed tests for the PACKET-06 COP engines."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claim_core import field_dictionary
from cop_runtime import PackLoadError
from cop_runtime.calcs import CalcInputsMissing
from cop_runtime.pack_loader import load_pack
from motor.calcs.calcs import (
    excess,
    pending_c04,
    pending_c07,
    pending_c08,
    reserve,
    reserve_breakdown,
    savings,
    write_off_reserve,
)

REPO = Path(__file__).resolve().parents[2]
MOTOR_PACK = REPO / "packs" / "motor"


def _copy_pack(tmp_path: Path, version: str) -> Path:
    copied = tmp_path / f"motor-{version}"
    shutil.copytree(MOTOR_PACK, copied)
    manifest = copied / "pack.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("version: 1.0.0", f"version: {version}"),
        encoding="utf-8",
    )
    return copied


def test_pack_calculations_cover_all_live_and_blocked_slots() -> None:
    assert excess(400_000_00) == 15_000_00
    assert excess(2_000_000_00) == 50_000_00
    assert excess(6_000_000_00) == 100_000_00
    assert reserve(10_00, 2_00, 1_00) == 13_00
    parent = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    lines = reserve_breakdown(
        100_00,
        10_00,
        5_00,
        "garage",
        "assessor",
        {"lines": [{"payee_party_id": "supplier", "amount": 25_00}]},
        parent,
    )
    assert sum(line["amount"] for line in lines) == 115_00
    assert all(line["parent_reserve_id"] == parent for line in lines)
    with pytest.raises(CalcInputsMissing):
        reserve_breakdown(
            100_00, 10_00, 5_00, "garage", "assessor", {}, parent
        )
    with pytest.raises(CalcInputsMissing):
        reserve_breakdown(
            100_00,
            10_00,
            5_00,
            "garage",
            "assessor",
            {"lines": [{"payee_party_id": "supplier", "amount": True}]},
            parent,
        )
    assert savings(200_00, 150_00) == 50_00
    with pytest.raises(CalcInputsMissing):
        savings(
            200_00,
            150_00,
            {"lines": [{"payee_party_id": "supplier", "amount": 70_00}]},
        )
    assert write_off_reserve(100_00, 10_00, 5_00) == 115_00
    assert pending_c04() is None
    assert pending_c07() is None
    assert pending_c08() is None


def test_loader_rejects_bad_yaml(tmp_path: Path) -> None:
    bad = _copy_pack(tmp_path, "1.0.2")
    (bad / "rules" / "R-01.yaml").write_text("id: [", encoding="utf-8")
    with pytest.raises(PackLoadError, match="Invalid YAML"):
        load_pack(bad)


def test_loader_rejects_unknown_rule_keys(tmp_path: Path) -> None:
    bad = _copy_pack(tmp_path, "1.0.3")
    rule = bad / "rules" / "R-01.yaml"
    rule.write_text(rule.read_text(encoding="utf-8") + "surprise: true\n", encoding="utf-8")
    with pytest.raises(PackLoadError, match="meta-schema"):
        load_pack(bad)


def test_loader_rejects_jsonlogic_compile_failure_without_registering_fields(
    tmp_path: Path,
) -> None:
    bad = _copy_pack(tmp_path, "1.0.4")
    unique_path = "test.packet06.partial_registration"
    fields = bad / "fields.yaml"
    fields.write_text(
        fields.read_text(encoding="utf-8")
        + f"  {unique_path}:\n    value_type: string\n    pii_class: none\n",
        encoding="utf-8",
    )
    rule = bad / "rules" / "R-03.yaml"
    rule.write_text(
        rule.read_text(encoding="utf-8").replace("when: true", "when: {unknown: []}"),
        encoding="utf-8",
    )
    assert unique_path not in field_dictionary()
    with pytest.raises(PackLoadError, match="unsupported JSONLogic"):
        load_pack(bad)
    assert unique_path not in field_dictionary()


def test_loader_rejects_attribute_escape_in_calc_module(tmp_path: Path) -> None:
    bad = _copy_pack(tmp_path, "1.0.5")
    calcs = bad / "calcs" / "calcs.py"
    calcs.write_text(
        calcs.read_text(encoding="utf-8") + "\nunsafe = (1).__class__\n",
        encoding="utf-8",
    )
    with pytest.raises(PackLoadError, match="attribute access"):
        load_pack(bad)


def test_unregistered_context_fields_are_visible_metadata() -> None:
    loaded = load_pack(MOTOR_PACK)
    rule = loaded.rule_registry.get("R-02")
    assert rule.pending_field_registration == (
        "client.loss_ratio",
        "client.premium_history",
    )
