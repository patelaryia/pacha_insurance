#!/usr/bin/env python3
"""Git and GitHub, with every result verified rather than assumed.

The previous design pushed without checking the push succeeded, stored the
text of a failed `gh pr create` as if it were a URL, and moved the packet to
`review` regardless. Every function here either returns a verified fact or
raises. There is no path that reports success it did not confirm.

Identities
----------
Two credentials, both optional, both read from the environment by name so
no token is ever stored in the repo:

- `github.builder_token_env` — the builder pushes and opens PRs as this.
- `github.reviewer_token_env` — the reviewer approves as this.

When both are set and they differ, an approval is a real GitHub approval and
a merge is a legitimate merge. When they are not set, the loop still runs;
it just stops at `merge_ready` and the owner merges. Nothing here ever
passes `--admin`.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]


class ForgeError(Exception):
    """A git or GitHub operation did not do what it was asked to do."""


def _run(
    cmd: list[str],
    cwd: pathlib.Path,
    *,
    token_env: str | None = None,
    check: bool = True,
    timeout: int = 180,
) -> str:
    env = dict(os.environ)
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ForgeError(f"{token_env} is not set — cannot act as that identity")
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
        if cmd and cmd[0] == "git":
            # GH_TOKEN is a gh CLI convention, not git authentication. Supply
            # the token through an ephemeral config environment and explicitly
            # reset credential helpers so agent-written repo config cannot run
            # arbitrary helpers in the privileged controller process.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                {
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "credential.helper",
                    "GIT_CONFIG_VALUE_0": "",
                    "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {basic}",
                }
            )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ForgeError(f"{' '.join(cmd[:3])}… exceeded {timeout}s") from exc
    if check and proc.returncode != 0:
        raise ForgeError(
            f"{' '.join(cmd[:3])}… exited {proc.returncode}\n{proc.stdout}{proc.stderr}".strip()
        )
    return (proc.stdout + proc.stderr).strip()


def git(args: list[str], cwd: pathlib.Path, *, check: bool = True) -> str:
    """Plain local git in a repo or controller-owned audit worktree."""
    return _run(["git", *args], cwd, check=check)


# --- worktrees ---------------------------------------------------------------


def resolve_base(
    repo: pathlib.Path,
    ref: str = "origin/main",
    *,
    token_env: str | None = None,
) -> str:
    _run(
        ["git", "fetch", "--quiet", "origin", "main"],
        repo,
        token_env=token_env,
    )
    return _run(["git", "rev-parse", ref], repo)


def _normalise_github_repo(url: str) -> str | None:
    match = re.match(
        r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+/[^/]+?)(?:\.git)?$",
        url.strip(),
    )
    return match.group(1) if match else None


def verify_repo_identity(worktree: pathlib.Path, expected_repository: str) -> None:
    """Refuse controller network actions if an agent changed git trust data."""
    remote = _run(["git", "remote", "get-url", "origin"], worktree)
    actual = _normalise_github_repo(remote)
    if actual != expected_repository:
        raise ForgeError(
            f"origin points to {remote!r}, expected GitHub repository "
            f"{expected_repository!r}"
        )
    suspicious = _run(
        [
            "git",
            "config",
            "--local",
            "--get-regexp",
            r"^(credential\.|core\.sshCommand$|http\..*extraHeader$|url\..*insteadOf$)",
        ],
        worktree,
        check=False,
    )
    if suspicious:
        raise ForgeError(
            "worktree contains controller-sensitive local git configuration; "
            f"refusing network access:\n{suspicious}"
        )


def ensure_worktree(repo: pathlib.Path, worktree: pathlib.Path, branch: str,
                    base_sha: str) -> str:
    """Create the worktree on first attempt, reuse it on every later one.

    Reuse is the point. A rework must keep the builder's prior work so the
    reviewer's findings apply to something, and a retry after a failed gate
    must keep it so the builder can see what it already tried. The previous
    design used `git worktree add -B` every attempt, which either failed
    outright or reset the branch and discarded the work under review.

    Returns "created" or "reused" so the caller can log which happened.
    """
    if worktree.exists():
        actual = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree)
        if actual != branch:
            raise ForgeError(
                f"worktree {worktree} is on {actual!r}, expected {branch!r}. "
                f"Refusing to touch it — resolve by hand."
            )
        return "reused"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    existing = _run(["git", "branch", "--list", branch], repo)
    if existing:
        _run(["git", "worktree", "add", str(worktree), branch], repo)
    else:
        _run(["git", "worktree", "add", "-b", branch, str(worktree), base_sha], repo)
    return "created"


def ensure_review_worktree(
    repo: pathlib.Path,
    worktree: pathlib.Path,
    head_sha: str,
) -> None:
    """Materialise an exact, detached, transcript-free checkout for review."""
    if worktree.exists():
        actual = _run(["git", "rev-parse", "HEAD"], worktree, check=False)
        dirt = _run(["git", "status", "--porcelain"], worktree, check=False)
        if actual == head_sha and not dirt:
            return
        remove_worktree(repo, worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree), head_sha], repo)


def remove_worktree(repo: pathlib.Path, worktree: pathlib.Path) -> None:
    if worktree.exists():
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo, check=False)
    shutil.rmtree(worktree, ignore_errors=True)
    _run(["git", "worktree", "prune"], repo, check=False)


def ensure_audit_worktree(
    repo: pathlib.Path,
    worktree: pathlib.Path,
    branch: str,
    base_sha: str,
    *,
    token_env=None,
) -> None:
    """Create or reuse the controller-only audit branch worktree."""
    _run(
        ["git", "fetch", "--quiet", "origin", branch],
        repo,
        token_env=token_env,
        check=False,
    )
    if worktree.exists():
        actual = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree)
        if actual != branch:
            raise ForgeError(
                f"audit worktree is on {actual!r}, expected {branch!r}"
            )
        remote = _run(["git", "branch", "-r", "--list", f"origin/{branch}"], repo)
        if remote:
            local_head = _run(["git", "rev-parse", "HEAD"], worktree)
            remote_head = _run(["git", "rev-parse", f"origin/{branch}"], worktree)
            if local_head != remote_head:
                # Audit files are a projection of the local ledger. Discarding
                # an unpublished audit-only commit is safe; it is immediately
                # regenerated, while resetting prevents replacing history
                # published by another scheduler host.
                _run(["git", "reset", "--hard", f"origin/{branch}"], worktree)
        return
    local = _run(["git", "branch", "--list", branch], repo)
    remote = _run(["git", "branch", "-r", "--list", f"origin/{branch}"], repo)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if local:
        _run(["git", "worktree", "add", str(worktree), branch], repo)
    elif remote:
        _run(
            ["git", "worktree", "add", "-b", branch, str(worktree), f"origin/{branch}"],
            repo,
        )
    else:
        _run(["git", "worktree", "add", "-b", branch, str(worktree), base_sha], repo)


def read_audit_snapshot(
    repo: pathlib.Path,
    branch: str,
    *,
    token_env=None,
) -> dict:
    _run(
        ["git", "fetch", "--quiet", "origin", branch],
        repo,
        token_env=token_env,
    )
    raw = _run(
        ["git", "show", f"origin/{branch}:audit/loop-state.json"],
        repo,
    )
    return json.loads(raw)


def is_clean(worktree: pathlib.Path) -> tuple[bool, str]:
    """A dirty worktree means the builder left work uncommitted, which the
    diff-against-base would not have seen. That is how the previous design
    could publish a green PR for code that was never committed.

    `.loop/` is excluded: it is the controller's own channel into the
    worktree (the builder's result file lives there) and is deliberately
    never committed. Everything else counts.
    """
    out = _run(["git", "status", "--porcelain"], worktree)
    dirt = [line for line in out.splitlines()
            if not line[3:].lstrip('"').startswith(".loop/")]
    return (not dirt, "\n".join(dirt))


def commits_ahead(worktree: pathlib.Path, base_sha: str) -> int:
    out = _run(["git", "rev-list", "--count", f"{base_sha}..HEAD"], worktree)
    return int(out)


def changed_paths(worktree: pathlib.Path, base_sha: str) -> list[str]:
    """Committed changes against the pinned base. Called only after
    `is_clean` has confirmed there is nothing else to miss."""
    out = _run(["git", "diff", "--name-only", f"{base_sha}...HEAD"], worktree)
    return [p for p in out.splitlines() if p]


# --- GitHub ------------------------------------------------------------------


def push(worktree: pathlib.Path, branch: str, *, token_env=None) -> str:
    _run(["git", "push", "--force-with-lease", "-u", "origin", branch],
         worktree, token_env=token_env)
    local = _run(["git", "rev-parse", "HEAD"], worktree)
    remote = _run(["git", "rev-parse", f"origin/{branch}"], worktree)
    if local != remote:
        raise ForgeError(f"push reported success but origin/{branch} is {remote}, HEAD is {local}")
    return local


def find_pr(worktree: pathlib.Path, branch: str, *, token_env=None) -> int | None:
    out = _run(["gh", "pr", "list", "--head", branch, "--state", "all",
                "--json", "number,state", "--limit", "5"],
               worktree, token_env=token_env)
    for pr in json.loads(out or "[]"):
        if pr["state"] == "OPEN":
            return pr["number"]
    return None


def ensure_pr(worktree: pathlib.Path, branch: str, title: str, body: str,
              *, token_env=None) -> int:
    """Open the PR, or return the one already open for this branch.

    Reuse matters: a rework pushes to the same branch, and opening a second
    PR for it would split the review history across two places.
    """
    existing = find_pr(worktree, branch, token_env=token_env)
    if existing:
        return existing
    _run(["gh", "pr", "create", "--head", branch, "--base", "main",
          "--title", title, "--body", body], worktree, token_env=token_env)
    number = find_pr(worktree, branch, token_env=token_env)
    if number is None:
        raise ForgeError(f"gh pr create reported success but no open PR exists for {branch}")
    return number


def ci_state(worktree: pathlib.Path, pr: int, required: list[str],
             *, token_env=None) -> dict:
    """The real green oracle: GitHub's check runs, not a local test run.

    Returns one of pending / green / red, plus which required contexts are
    missing or failing. A required context that has not reported is
    `pending`, never green — the previous design would have called an
    unreported check a pass.
    """
    out = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--json",
            "statusCheckRollup,mergeStateStatus,state,headRefOid,headRefName,baseRefName",
        ],
        worktree,
        token_env=token_env,
    )
    data = json.loads(out)
    runs = {}
    for check in data.get("statusCheckRollup") or []:
        name = check.get("name") or check.get("context")
        runs[name] = (check.get("conclusion") or check.get("state") or "PENDING").upper()

    failing = [n for n in required if runs.get(n) in ("FAILURE", "TIMED_OUT", "CANCELLED",
                                                      "ACTION_REQUIRED", "ERROR")]
    unreported = [n for n in required if n not in runs
                  or runs[n] in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "NONE")]
    if failing:
        verdict = "red"
    elif unreported:
        verdict = "pending"
    else:
        verdict = "green"
    return {"verdict": verdict, "failing": failing, "unreported": unreported,
            "runs": runs, "pr_state": data.get("state"),
            "merge_state": data.get("mergeStateStatus"),
            "head_sha": data.get("headRefOid"),
            "head_ref": data.get("headRefName"),
            "base_ref": data.get("baseRefName")}


def failing_logs(worktree: pathlib.Path, pr: int, limit: int = 4000,
                 *, token_env=None) -> str:
    """The failing job output, so a red CI rework tells the builder what
    broke rather than only that something did."""
    out = _run(["gh", "pr", "checks", str(pr), "--json", "name,state,link"],
               worktree, token_env=token_env, check=False)
    try:
        checks = json.loads(out or "[]")
    except json.JSONDecodeError:
        return out[:limit]
    lines = [f"{c['name']}: {c['state']}  {c.get('link','')}" for c in checks]
    run_log = _run(["gh", "run", "view", "--log-failed"], worktree,
                   token_env=token_env, check=False)
    return ("\n".join(lines) + "\n\n" + run_log)[:limit]


def pr_state(worktree: pathlib.Path, pr: int, *, token_env=None) -> dict:
    out = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--json",
            "state,mergedAt,mergeCommit,headRefOid,headRefName,baseRefName",
        ],
        worktree,
        token_env=token_env,
    )
    data = json.loads(out)
    return {"state": data["state"], "merged_at": data.get("mergedAt"),
            "merge_commit": (data.get("mergeCommit") or {}).get("oid"),
            "head_sha": data.get("headRefOid"),
            "head_ref": data.get("headRefName"),
            "base_ref": data.get("baseRefName")}


def verify_pr_identity(
    state: dict,
    *,
    expected_head_sha: str,
    expected_head_ref: str,
    expected_base_ref: str = "main",
) -> None:
    mismatches = []
    if state.get("head_sha") != expected_head_sha:
        mismatches.append(
            f"head {state.get('head_sha')!r} != validated {expected_head_sha!r}"
        )
    if state.get("head_ref") != expected_head_ref:
        mismatches.append(
            f"head ref {state.get('head_ref')!r} != {expected_head_ref!r}"
        )
    if state.get("base_ref") != expected_base_ref:
        mismatches.append(
            f"base ref {state.get('base_ref')!r} != {expected_base_ref!r}"
        )
    if mismatches:
        raise ForgeError("PR identity changed after validation: " + "; ".join(mismatches))


def approve(worktree: pathlib.Path, pr: int, body: str, *, token_env: str) -> None:
    """Approve as the reviewer identity. Requires a token distinct from the
    author's — GitHub rejects self-approval, which is the whole reason the
    two-identity design exists."""
    _run(["gh", "pr", "review", str(pr), "--approve", "--body", body],
         worktree, token_env=token_env)


def merge(worktree: pathlib.Path, pr: int, *, token_env: str) -> str:
    """Merge through the normal protected-branch path. No `--admin`: if the
    ruleset refuses, that refusal is information and the packet escalates."""
    _run(["gh", "pr", "merge", str(pr), "--squash", "--delete-branch"],
         worktree, token_env=token_env)
    state = pr_state(worktree, pr, token_env=token_env)
    if state["state"] != "MERGED":
        raise ForgeError(f"gh pr merge returned 0 but PR #{pr} is {state['state']}")
    return state["merge_commit"]
