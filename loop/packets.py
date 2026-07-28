#!/usr/bin/env python3
"""Packet files: immutable build specs plus one-time bootstrap state.

A packet declares what to build and what proves it. Runtime state lives in
`loop/store.py` and is published on the audit branch. Existing `status`, `pr`
and `attempts` keys seed a new ledger once so adoption does not resurrect
completed work; the controller never writes them during normal operation.

This split is the fix for the worst defect in the previous design: with the
builder running in a worktree, "the packet file is the source of truth"
silently meant two files on two filesystems.
"""
from __future__ import annotations

import fnmatch
import hashlib
import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
ORACLE_LOCK = REPO / "loop" / "oracle.lock"

# Written by the slicer, read by the controller, never written by an agent.
SPEC_FIELDS = ("id", "prd_ref", "title", "depends_on", "branch", "blast_radius",
               "acceptance_tests")
ID_RE = re.compile(r"^(PACKET-\d{2}|TEMPORAL-T\d{2})$")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class SpecError(Exception):
    """A packet file does not describe work the controller can dispatch."""


class Packet:
    def __init__(self, path: pathlib.Path, meta: dict, body: str):
        self.path = path
        self.meta = meta
        self.body = body

    @property
    def id(self) -> str:
        return self.meta["id"]

    def __repr__(self) -> str:
        return f"<Packet {self.id}>"


def parse(path: pathlib.Path) -> Packet:
    text = path.read_text()
    match = _FRONT_MATTER.match(text)
    if not match:
        raise SpecError(f"{path}: no YAML front matter — not a board packet")
    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise SpecError(f"{path}: front matter is not a mapping")
    validate_spec(meta, path)
    return Packet(path, meta, text[match.end():])


def validate_spec(meta: dict, path: pathlib.Path) -> None:
    missing = [f for f in SPEC_FIELDS if f not in meta]
    if missing:
        raise SpecError(f"{path}: front matter missing {', '.join(missing)}")
    if not ID_RE.match(str(meta["id"])):
        raise SpecError(f"{path}: id {meta['id']!r} is not PACKET-NN or TEMPORAL-TNN")
    if not isinstance(meta["depends_on"], list):
        raise SpecError(f"{path}: depends_on must be a list")
    if not isinstance(meta["blast_radius"], bool):
        raise SpecError(f"{path}: blast_radius must be a bool, not {meta['blast_radius']!r}")
    if not isinstance(meta["acceptance_tests"], list):
        raise SpecError(f"{path}: acceptance_tests must be a list")
    for rel in meta["acceptance_tests"]:
        candidate = pathlib.PurePosixPath(str(rel))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SpecError(f"{path}: acceptance test path escapes the repository: {rel!r}")
        if not candidate.parts or candidate.parts[0] != "tests":
            raise SpecError(f"{path}: acceptance test must live under tests/: {rel!r}")


def load_board(board_dir: pathlib.Path) -> list[Packet]:
    packets = [parse(p) for p in sorted(board_dir.glob("*.md"))]
    ids = [p.id for p in packets]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SpecError(f"duplicate packet ids on the board: {sorted(duplicates)}")
    known = set(ids)
    for packet in packets:
        unknown = [d for d in packet.meta["depends_on"] if d not in known]
        if unknown:
            raise SpecError(f"{packet.path}: depends_on names packets not on the board: {unknown}")
    return packets


def load_config(path: pathlib.Path | None = None) -> dict:
    return yaml.safe_load((path or REPO / "loop" / "config.yml").read_text())


# --- the acceptance oracle ---------------------------------------------------
#
# Directory names are not a protection boundary. The previous design told the
# builder "do not touch tests/" while the config protected only
# `tests/acceptance/**` — so a named acceptance test under tests/integration/
# was editable, and ED-7's required unit tests were forbidden by the prompt.
# Both halves were wrong.
#
# The oracle is an explicit file->hash map. Files in it are frozen. Files not
# in it — new unit and integration tests the builder must write to satisfy
# ED-7 — are free.


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_oracle() -> dict[str, str]:
    if not ORACLE_LOCK.exists():
        raise SpecError(
            "loop/oracle.lock is missing. The controller will not dispatch without a "
            "pinned definition of done — run `loop/controller.py oracle --update` and have "
            "the owner review the result."
        )
    raw = yaml.safe_load(ORACLE_LOCK.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), dict):
        raise SpecError("loop/oracle.lock must contain a files mapping")
    return raw["files"]


def build_oracle(board: list[Packet], root: pathlib.Path) -> dict[str, str]:
    """Hash every acceptance test any packet names. Missing files are an
    error, not an omission: a packet whose oracle does not exist cannot be
    dispatched, and silently skipping it would hide that."""
    files: dict[str, str] = {}
    missing: list[str] = []
    for packet in board:
        for rel in packet.meta["acceptance_tests"]:
            path = root / rel
            if not path.exists():
                missing.append(f"{packet.id}: {rel}")
                continue
            files[rel] = sha256(path)
    if missing:
        raise SpecError(
            "acceptance tests named by a packet do not exist:\n  "
            + "\n  ".join(missing)
            + "\nSlice them first (loop/slice.md) — the loop does not invent a "
              "definition of done."
        )
    return dict(sorted(files.items()))


def write_oracle(files: dict[str, str]) -> None:
    ORACLE_LOCK.write_text(
        "# The definition of done, pinned by content hash.\n"
        "#\n"
        "# Every file here is frozen. The controller verifies these hashes in the\n"
        "# builder's worktree after every attempt and refuses to open a PR if one\n"
        "# moved. Files NOT listed here — new unit and integration tests, which\n"
        "# ED-7 requires the builder to write — are free.\n"
        "#\n"
        "# Regenerate with `loop/controller.py oracle --update`. Changing a hash changes\n"
        "# the definition of done and needs the owner's review, which CODEOWNERS\n"
        "# already enforces on tests/acceptance/.\n"
        + yaml.safe_dump({"files": files}, sort_keys=False, default_flow_style=False)
    )


def oracle_violations(worktree: pathlib.Path, oracle: dict[str, str]) -> list[str]:
    """Frozen files that moved, or vanished, inside the builder's worktree."""
    broken = []
    for rel, expected in oracle.items():
        path = worktree / rel
        if not path.exists():
            broken.append(f"{rel} (deleted)")
        elif sha256(path) != expected:
            broken.append(f"{rel} (modified)")
    return broken


def oracle_differences(
    board: list[Packet],
    root: pathlib.Path,
    locked: dict[str, str],
) -> list[str]:
    """Prove the current board and the reviewed lock describe one oracle."""
    expected = build_oracle(board, root)
    differences = []
    for rel, digest in expected.items():
        if rel not in locked:
            differences.append(f"{rel} is named by the board but not pinned")
        elif locked[rel] != digest:
            differences.append(f"{rel} content does not match its pinned hash")
    for rel in locked:
        if rel not in expected:
            differences.append(f"{rel} is pinned but no packet names it")
    return differences


# --- blast radius ------------------------------------------------------------


def blast_radius_patterns() -> list[str]:
    return yaml.safe_load((REPO / "loop" / "blast-radius.yml").read_text())["paths"]


def matches(paths: list[str], patterns: list[str]) -> list[str]:
    """Paths falling inside a pattern list.

    `**` is also matched as `*`, which over-matches across separators. That
    is the safe direction here: a false positive costs a human merge, a
    false negative merges a migration unattended.
    """
    hits = []
    for path in paths:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**", "*")):
                hits.append(path)
                break
    return hits


def in_blast_radius(paths: list[str]) -> list[str]:
    return matches(paths, blast_radius_patterns())
