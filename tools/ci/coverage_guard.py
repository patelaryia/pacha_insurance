#!/usr/bin/env python3
"""ED-7a coverage boundaries — the definition-of-done thresholds, enforced.

Section 0, ED-7a fixes two separate rules, and they are not the same number:

  * `platform/` and `agents/` together must reach **>= 80%**, aggregated —
    a single thin module does not fail the build on its own;
  * every `packs/**/calcs.py` must reach **100%** individually. Calc code is
    the money path (ED-8), so a partially covered branch there is a defect.

`infra/` and Alembic migrations are out of scope and never counted.

Input is a `coverage json` report (coverage.py format 3, branch coverage on).
Coverage is measured the way coverage.py measures it: covered statements plus
covered branches over total statements plus total branches.

A `packs/**/calcs.py` file that exists on disk but is missing from the report is
a failure, not a pass — an uncollected calc module would otherwise score 100%
by never being measured.

Exit code 1 on any violation, printing one `path: ...` line per finding.
No third-party deps.
"""
from __future__ import annotations

import json
import pathlib
import sys

AGGREGATE_ROOTS = ("platform/", "agents/")
AGGREGATE_FLOOR = 80.0
CALCS_GLOB = "packs/**/calcs.py"


def _percent(covered: int, total: int) -> float:
    """coverage.py's own percentage: an empty measured set is 100%."""
    if total == 0:
        return 100.0
    return 100.0 * covered / total


def aggregate_percent(files: dict[str, dict], roots: tuple[str, ...]) -> tuple[float, int]:
    """Combined statement+branch coverage across every file under `roots`."""
    covered = total = counted = 0
    for path, entry in files.items():
        if not path.startswith(roots):
            continue
        summary = entry["summary"]
        covered += summary["covered_lines"] + summary["covered_branches"]
        total += summary["num_statements"] + summary["num_branches"]
        counted += 1
    return _percent(covered, total), counted


def file_percent(entry: dict) -> float:
    """Combined statement+branch coverage for one file's report entry."""
    summary = entry["summary"]
    covered = summary["covered_lines"] + summary["covered_branches"]
    total = summary["num_statements"] + summary["num_branches"]
    return _percent(covered, total)


def discover_calcs(root: pathlib.Path) -> list[str]:
    """Every `packs/**/calcs.py` on disk, as repo-relative POSIX paths."""
    return sorted(p.relative_to(root).as_posix() for p in root.glob(CALCS_GLOB))


def check(report: dict, calcs_files: list[str]) -> list[str]:
    """Return one message per ED-7a violation; empty list means green."""
    findings: list[str] = []
    files = report["files"]

    percent, counted = aggregate_percent(files, AGGREGATE_ROOTS)
    if counted == 0:
        findings.append(
            "platform/ + agents/: no files in the coverage report — "
            "the run measured nothing in scope"
        )
    elif percent < AGGREGATE_FLOOR:
        findings.append(
            f"platform/ + agents/: {percent:.2f}% is below the ED-7a floor "
            f"of {AGGREGATE_FLOOR:.0f}% ({counted} files)"
        )

    for path in calcs_files:
        entry = files.get(path)
        if entry is None:
            findings.append(
                f"{path}: absent from the coverage report — ED-7a requires 100%, "
                "and an unmeasured calc module is not covered"
            )
            continue
        percent = file_percent(entry)
        if percent < 100.0:
            findings.append(
                f"{path}: {percent:.2f}% — ED-7a requires 100% for packs/**/calcs.py"
            )
    return findings


def main(argv: list[str]) -> int:
    report_path = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path("coverage.json")
    root = pathlib.Path(__file__).resolve().parents[2]
    if not report_path.exists():
        print(f"{report_path}: coverage report not found", file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text())
    findings = check(report, discover_calcs(root))
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
