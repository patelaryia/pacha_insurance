#!/usr/bin/env python3
"""Publish the controller ledger without ever committing onto local main."""
from __future__ import annotations

import hashlib
import json
import pathlib
import time

import forge
import store

REPO = pathlib.Path(__file__).resolve().parents[1]


def publish(conn, config: dict, digest_text: str, *, token_env=None) -> str | None:
    audit = config["audit"]
    if not audit.get("enabled"):
        return None
    branch = audit["branch"]
    worktree = REPO / ".claude" / "worktrees" / "loop-audit"
    base_sha = forge.resolve_base(REPO, token_env=token_env)
    forge.ensure_audit_worktree(
        REPO,
        worktree,
        branch,
        base_sha,
        token_env=token_env,
    )
    forge.verify_repo_identity(worktree, config["github"]["repository"])

    target = worktree / "audit"
    target.mkdir(parents=True, exist_ok=True)
    snapshot = store.export_state(conn)
    pause = REPO / "loop" / "PAUSED"
    config_bytes = json.dumps(config, sort_keys=True).encode()
    snapshot["controller"] = {
        "source_head": forge.git(["rev-parse", "HEAD"], REPO),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "pause": pause.read_text().strip() if pause.exists() else None,
    }
    snapshot_text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    snapshot_path = target / "loop-state.json"
    if snapshot_path.exists() and snapshot_path.read_text() == snapshot_text:
        return None
    snapshot_path.write_text(snapshot_text)
    with (target / "events.jsonl").open("w") as handle:
        for event in snapshot["events"]:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    (target / "digest.md").write_text(digest_text)
    (target / "README.md").write_text(
        "# Pacha autonomous-loop audit\n\n"
        "Controller-owned state published from `loop/state.db`.\n\n"
        "- `loop-state.json` is a complete recovery snapshot.\n"
        "- `events.jsonl` is the append-only transition history.\n"
        "- `digest.md` is the current human-readable view.\n"
    )

    changed = forge.git(["status", "--porcelain", "--", "audit"], worktree)
    if not changed:
        return None
    forge.git(["add", "audit"], worktree)
    forge.git(
        [
            "commit",
            "-m",
            f"loop audit: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "--no-verify",
        ],
        worktree,
    )
    return forge.push(worktree, branch, token_env=token_env)


def restore(conn, config: dict, *, token_env=None) -> None:
    branch = config["audit"]["branch"]
    snapshot = forge.read_audit_snapshot(REPO, branch, token_env=token_env)
    store.restore_state(conn, snapshot)
