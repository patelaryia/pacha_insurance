#!/usr/bin/env python3
"""AR-2 banned-call check — one choke point for all side effects.

AGENT_BUILD_GUIDE invariant 4 / Section 0.5 AR-2: "Zero direct sends/writes
outside `execute_or_stage`. CI greps for `graph_client.send` and adapter
`.execute` outside the gate module; `notify/` is the only whitelisted path
(AR-5)."

This checker scans `platform/`, `agents/`, `packs/`, `console/` for banned
direct side-effect calls and fails unless the file is exempt:

  * the gate module — the file that defines `execute_or_stage`, or
  * anything under a `notify/` directory (AR-5 exemption).

Exit code 1 on any violation. No third-party deps.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOTS = ("platform", "agents", "packs", "console")
EXTS = {".py", ".ts", ".tsx", ".js", ".jsx"}

PATTERNS = (
    (re.compile(r"\bgraph_client\.send\b"), "direct graph_client.send"),
    (re.compile(r"\badapter\.execute\b"), "direct adapter.execute"),
    (re.compile(r"\.execute_op\s*\("), "direct adapter execute_op"),
)

GATE_DEF = re.compile(r"\bdef\s+execute_or_stage\b")


def is_exempt(rel: pathlib.Path, text: str) -> bool:
    if "notify" in rel.parts:  # AR-5 whitelisted send path
        return True
    if GATE_DEF.search(text):  # the gate module itself
        return True
    return False


def scan_text(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*")):
            continue
        for pat, msg in PATTERNS:
            if pat.search(line):
                hits.append((i, msg))
    return hits


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parents[2]
    failed = False
    for root in ROOTS:
        base = repo / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in EXTS:
                continue
            rel = path.relative_to(repo)
            text = path.read_text(encoding="utf-8", errors="replace")
            if is_exempt(rel, text):
                continue
            for lineno, msg in scan_text(text):
                failed = True
                print(f"{rel}:{lineno}: AR-2 banned-call: {msg} (route through execute_or_stage)")

    if failed:
        print("\nAR-2 violated: all side effects go through execute_or_stage (notify/ exempt).")
        return 1
    print("banned-calls check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
