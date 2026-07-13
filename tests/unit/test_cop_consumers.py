"""Fail-closed unit coverage for PACKET-07 template and routing loaders."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from cop_runtime import PackLoadError
from cop_runtime.routing import load_authority_matrix
from cop_runtime.templates import (
    TemplateRegistry,
    load_template_registry,
    money_kes_display,
)


def _write_yaml(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [{"max": 1, "role": "a"}],
        [{"max": None, "role": ""}],
        [{"max": None}],
        [{"max": None, "role": "a", "extra": True}],
        [{"max": None, "role": "a", "side_effects": [1]}],
        [{"max": -1, "role": "a"}, {"max": None, "role": "b"}],
        [{"max": True, "role": "a"}, {"max": None, "role": "b"}],
        [{"max": "1", "role": "a"}, {"max": None, "role": "b"}],
        [{"max": 2, "role": "a"}, {"max": 2, "role": "b"}, {"max": None, "role": "c"}],
        [{"max": None, "role": "a"}, {"max": None, "role": "b"}],
    ],
)
def test_authority_matrix_rejects_every_malformed_shape(
    tmp_path: Path, payload
) -> None:
    path = _write_yaml(tmp_path / "authority.yaml", payload)
    with pytest.raises(PackLoadError):
        load_authority_matrix(path)


def test_authority_matrix_rejects_non_integer_route_amount(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path / "authority.yaml", [{"max": None, "role": "a"}])
    matrix = load_authority_matrix(path)
    with pytest.raises(TypeError):
        matrix.route("100")  # type: ignore[arg-type]


def _live_row() -> dict:
    return {
        "id": "T-90",
        "version": "1.0.0",
        "channel": "email",
        "body_ref": "T-90.j2",
        "required_fields": ["reserve.total"],
        "min_verification": "extracted",
        "locale": "en-KE",
        "status": "live",
    }


def _load_registry(tmp_path: Path, payload) -> TemplateRegistry:
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "T-90.j2").write_text("{{ reserve_total }}", encoding="utf-8")
    path = _write_yaml(templates / "registry.yaml", payload)
    return load_template_registry(
        path,
        calc_ids={"C-08"},
        known_fields={"reserve.total"},
    )


def _invalid_template_payloads() -> list[object]:
    row = _live_row()
    mutations = []
    for key, value in (
        ("id", "bad"),
        ("version", "1"),
        ("channel", "sms"),
        ("required_fields", "reserve.total"),
        ("required_fields", ["reserve.total", "reserve.total"]),
        ("required_fields", ["unknown.path"]),
        ("min_verification", "system_confirmed"),
        ("locale", "sw-KE"),
        ("status", "draft"),
        ("calc_slots", {"bad-name": "C-08"}),
        ("calc_slots", {"payable": "C-99"}),
        ("calc_slots", {"reserve_total": "C-08"}),
    ):
        changed = deepcopy(row)
        changed[key] = value
        mutations.append({"templates": [changed]})
    missing = deepcopy(row)
    missing.pop("version")
    mutations.append({"templates": [missing]})
    extra = deepcopy(row)
    extra["surprise"] = True
    mutations.append({"templates": [extra]})
    live_blocked = deepcopy(row)
    live_blocked["blocked_on"] = ["not allowed"]
    mutations.append({"templates": [live_blocked]})
    pending = deepcopy(row)
    pending.update(status="pending_capture", body_ref=None)
    mutations.append({"templates": [pending]})
    field_set = deepcopy(row)
    field_set.update(channel="field_set", body_ref="T-90.j2")
    mutations.append({"templates": [field_set]})
    duplicate = deepcopy(row)
    mutations.append({"templates": [row, duplicate]})
    return [None, [], {"wrong": []}, {"templates": {}}, *mutations]


@pytest.mark.parametrize("payload", _invalid_template_payloads())
def test_template_registry_rejects_malformed_metadata(
    tmp_path: Path, payload
) -> None:
    with pytest.raises(PackLoadError):
        _load_registry(tmp_path, payload)


def test_template_registry_rejects_missing_and_escaping_bodies(tmp_path: Path) -> None:
    row = _live_row()
    row["body_ref"] = "missing.j2"
    with pytest.raises(PackLoadError):
        _load_registry(tmp_path, {"templates": [row]})

    row["body_ref"] = "../outside.j2"
    (tmp_path / "outside.j2").write_text("outside", encoding="utf-8")
    with pytest.raises(PackLoadError):
        _load_registry(tmp_path, {"templates": [row]})


def test_template_registry_defaults_locale_and_unknown_id_refuses(tmp_path: Path) -> None:
    row = _live_row()
    row.pop("locale")
    registry = _load_registry(tmp_path, {"templates": [row]})
    assert registry.get("T-90").locale == "en-KE"
    with pytest.raises(LookupError):
        registry.get("T-99")


def test_money_display_is_integer_only_and_handles_sign() -> None:
    assert money_kes_display(123_456) == "KES 1,234.56"
    assert money_kes_display(-1) == "KES -0.01"
    with pytest.raises(TypeError):
        money_kes_display(True)
