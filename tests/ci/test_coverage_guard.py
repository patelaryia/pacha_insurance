"""Meta-tests: the ED-7a coverage guard must fail on the boundaries it enforces.

A guard that cannot fail is not a gate, so these pin both thresholds, the
80% boundary itself, and the unmeasured-calc-module case.
"""
from __future__ import annotations

import importlib.util
import pathlib

_CI = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ci"
_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CI / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


guard = _load("coverage_guard")


def _entry(statements: int, covered: int, branches: int = 0, covered_branches: int = 0) -> dict:
    return {
        "summary": {
            "num_statements": statements,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


def _report(**files: dict) -> dict:
    return {"files": dict(files)}


# --- aggregate platform/ + agents/ floor ---------------------------------------

def test_aggregate_below_floor_fails():
    report = _report(**{"platform/a.py": _entry(100, 79)})
    findings = guard.check(report, [])
    assert findings and "below the ED-7a floor" in findings[0]


def test_aggregate_exactly_eighty_percent_passes():
    report = _report(**{"platform/a.py": _entry(100, 80)})
    assert guard.check(report, []) == []


def test_aggregate_just_under_eighty_percent_fails():
    report = _report(**{"platform/a.py": _entry(10_000, 7_999)})
    assert guard.check(report, []) != []


def test_aggregate_pools_platform_and_agents():
    """A thin low module does not fail the build while the pool clears 80%."""
    report = _report(
        **{
            "platform/a.py": _entry(90, 90),
            "agents/b.py": _entry(10, 0),
        }
    )
    assert guard.check(report, []) == []


def test_aggregate_counts_partial_branches():
    """Every statement hit is not 100% while branches go untaken."""
    report = _report(**{"platform/a.py": _entry(80, 80, branches=30, covered_branches=0)})
    findings = guard.check(report, [])
    assert findings and "72.73%" in findings[0]


def test_aggregate_ignores_out_of_scope_roots():
    """infra/ and packs/ are out of the ED-7a aggregate, high or low."""
    report = _report(
        **{
            "platform/a.py": _entry(100, 100),
            "infra/c.py": _entry(100, 0),
            "packs/motor/d.py": _entry(100, 0),
        }
    )
    assert guard.check(report, []) == []


def test_empty_in_scope_report_fails():
    """A run that measured nothing must not read as a pass."""
    report = _report(**{"infra/c.py": _entry(10, 10)})
    findings = guard.check(report, [])
    assert findings and "measured nothing in scope" in findings[0]


# --- packs/**/calcs.py must be 100% -------------------------------------------

def test_calcs_at_one_hundred_percent_passes():
    report = _report(
        **{
            "platform/a.py": _entry(100, 100),
            "packs/motor/calcs/calcs.py": _entry(50, 50, branches=8, covered_branches=8),
        }
    )
    assert guard.check(report, ["packs/motor/calcs/calcs.py"]) == []


def test_calcs_one_line_short_fails():
    report = _report(
        **{
            "platform/a.py": _entry(100, 100),
            "packs/motor/calcs/calcs.py": _entry(50, 49),
        }
    )
    findings = guard.check(report, ["packs/motor/calcs/calcs.py"])
    assert findings and "requires 100%" in findings[0]


def test_calcs_partial_branch_fails():
    report = _report(
        **{
            "platform/a.py": _entry(100, 100),
            "packs/motor/calcs/calcs.py": _entry(50, 50, branches=8, covered_branches=7),
        }
    )
    assert guard.check(report, ["packs/motor/calcs/calcs.py"]) != []


def test_calcs_absent_from_report_fails():
    """An uncollected calc module would otherwise score 100% by not being run."""
    report = _report(**{"platform/a.py": _entry(100, 100)})
    findings = guard.check(report, ["packs/motor/calcs/calcs.py"])
    assert findings and "absent from the coverage report" in findings[0]


# --- discovery ----------------------------------------------------------------

def test_discovery_finds_the_repo_calcs_modules():
    discovered = guard.discover_calcs(_ROOT)
    assert "packs/motor/calcs/calcs.py" in discovered
    assert all(path.endswith("/calcs.py") for path in discovered)
