#!/usr/bin/env python3
"""Preflight, and the fast local gate.

Two different jobs, deliberately not merged:

**Preflight** proves the machine can run a build at all — interpreter,
linters, dependencies, git, gh, both identities. It runs once per tick,
before any packet is selected, and a failure pauses the whole loop with a
named cause. The previous design had no preflight: a machine that could not
run `ruff` produced `infra_error` on every packet, which by design did not
trip the bad-slice breaker, so the loop failed quietly for as long as you
let it.

**The fast gate** is a cheap local filter so an obviously-broken attempt
never reaches GitHub. It is NOT the definition of green. GitHub's six
required check runs are, and `forge.ci_state` is what reads them. Running
the PostgreSQL tier locally would double the one job that gates CI wall
time, on a laptop whose timings are not comparable anyway.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import forge

REPO = pathlib.Path(__file__).resolve().parents[1]


def interpreter(config: dict) -> str:
    return os.environ.get("LOOP_PYTHON") or config["builder"]["python"]


def _run(
    cmd: list[str],
    cwd: pathlib.Path,
    timeout: int = 900,
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        return 127, f"{cmd[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd[:3])}… exceeded {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr)


def ruff_cmd(python: str) -> list[str]:
    """Prefer `<python> -m ruff` so LOOP_PYTHON actually selects the ruff the
    gate runs. Fall back to the standalone binary only if the module is not
    importable — the previous design always invoked the bare executable, so
    pointing LOOP_PYTHON at a venv silently did nothing for lint."""
    code, _ = _run([python, "-c", "import ruff"], REPO, timeout=60)
    if code == 0:
        return [python, "-m", "ruff"]
    if shutil.which("ruff"):
        return ["ruff"]
    return []


# --- preflight ---------------------------------------------------------------


def preflight(config: dict, *, check_identities: bool = True) -> list[str]:
    """Everything that must be true before any packet is dispatched.

    Returns a list of failures in prose. Empty means go. The controller
    pauses the loop on any failure rather than letting packets fail one at
    a time against a broken machine.
    """
    python = interpreter(config)
    problems: list[str] = []

    code, out = _run([python, "-c", "import sys; print(sys.version_info[:2])"], REPO, timeout=60)
    if code != 0:
        problems.append(f"interpreter {python!r} does not run: {out.strip()[:200]}")
        return problems  # nothing else is meaningful without an interpreter

    code, out = _run([python, "-c",
                      "import sys;"
                      "sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)"], REPO, timeout=60)
    if code != 0:
        problems.append(
            f"{python} is older than 3.12, which pyproject.toml requires. A gate that "
            f"passes under the wrong interpreter is not a gate. Set LOOP_PYTHON."
        )

    if not ruff_cmd(python):
        problems.append(
            f"ruff is neither importable by {python} nor on PATH. Lint is a required "
            f"CI context, so a build that cannot lint locally cannot be pre-filtered."
        )

    code, out = _run([python, "-c", "import pytest, yaml, fastapi, sqlalchemy"], REPO, timeout=120)
    if code != 0:
        problems.append(f"{python} cannot import the project dependencies: {out.strip()[:200]}")

    for tool in ("git", "gh", config["builder"]["command"], config["reviewer"]["command"]):
        if not shutil.which(tool):
            problems.append(f"{tool!r} is not on PATH")

    github = config.get("github") or {}
    configured_tokens = [
        name
        for name in (
            github.get("builder_token_env"),
            github.get("reviewer_token_env"),
        )
        if name
    ]
    if shutil.which("gh") and not configured_tokens:
        code, out = _run(["gh", "auth", "status"], REPO, timeout=60)
        if code != 0:
            problems.append(f"gh is not authenticated: {out.strip()[:200]}")

    code, out = _run(["git", "rev-parse", "origin/main"], REPO, timeout=60)
    if code != 0:
        problems.append("origin/main is not resolvable — fetch first")

    code, out = _run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "loop",
            ".github",
            ".claude/skills",
            ".codex/skills",
            "tools/ci",
            "docs/packets",
            "AGENTS.md",
            "CLAUDE.md",
        ],
        REPO,
        timeout=60,
    )
    if code != 0:
        problems.append(f"could not inspect controller-owned paths: {out.strip()[:200]}")
    elif out.strip():
        problems.append(
            "controller-owned specs or governance are uncommitted; refusing to "
            f"run mutable controller code:\n{out.strip()[:1000]}"
        )

    if check_identities:
        problems += identity_problems(config)
        problems += live_identity_problems(config)

    try:
        forge.verify_repo_identity(REPO, github["repository"])
    except (forge.ForgeError, KeyError) as exc:
        problems.append(f"repository identity check failed: {exc}")

    return problems


def identity_problems(config: dict) -> list[str]:
    """Identity configuration must be coherent or absent, never half-done.

    Half-configured is the dangerous state: a reviewer token that is
    actually the owner's produces an approval GitHub silently rejects, and
    the loop would wait forever for a merge that cannot happen.
    """
    github = config.get("github") or {}
    builder_env = github.get("builder_token_env")
    reviewer_env = github.get("reviewer_token_env")
    problems = []
    for name in (builder_env, reviewer_env):
        if name and not os.environ.get(name):
            problems.append(f"configured GitHub credential ${name} is not set")
    if (
        builder_env
        and reviewer_env
        and os.environ.get(builder_env)
        and os.environ.get(builder_env) == os.environ.get(reviewer_env)
    ):
        problems.append(
            f"${builder_env} and ${reviewer_env} hold the same token. GitHub "
            f"forbids approving your own pull request, so this would deadlock."
        )
    if github.get("auto_merge"):
        if not (builder_env and reviewer_env):
            problems.append(
                "github.auto_merge is on but builder_token_env / reviewer_token_env are not "
                "both set. Auto-merge requires two distinct identities; a single identity "
                "cannot approve its own PR and the loop does not use --admin."
            )
    return problems


def _github_identity(token_env: str) -> tuple[str | None, str | None]:
    """Resolve a token to its GitHub actor without exposing the token."""
    token = os.environ.get(token_env)
    if not token:
        return None, f"${token_env} is not set"
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    code, out = _run(["gh", "api", "user", "--jq", ".login"], REPO, 60, env=env)
    if code == 0 and out.strip():
        return f"user:{out.strip()}", None
    code, out = _run(
        ["gh", "api", "/installation", "--jq", ".app_slug"],
        REPO,
        60,
        env=env,
    )
    if code == 0 and out.strip():
        return f"app:{out.strip()}", None
    return None, (
        f"${token_env} is not a valid GitHub user or installation token: "
        f"{out.strip()[:200]}"
    )


def live_identity_problems(config: dict) -> list[str]:
    """Prove configured credentials are usable and resolve to different actors."""
    github = config.get("github") or {}
    names = [
        name
        for name in (
            github.get("builder_token_env"),
            github.get("reviewer_token_env"),
        )
        if name and os.environ.get(name)
    ]
    if not names or not shutil.which("gh"):
        return []
    problems = []
    identities = {}
    for name in names:
        identity, problem = _github_identity(name)
        if problem:
            problems.append(problem)
        elif identity:
            identities[name] = identity
        env = dict(os.environ)
        env["GH_TOKEN"] = os.environ[name]
        env["GITHUB_TOKEN"] = os.environ[name]
        code, out = _run(
            ["gh", "api", f"repos/{github['repository']}", "--jq", ".full_name"],
            REPO,
            60,
            env=env,
        )
        if code != 0 or out.strip().lower() != github["repository"].lower():
            problems.append(
                f"${name} cannot read the configured repository "
                f"{github['repository']!r}: {out.strip()[:200]}"
            )
    if len(set(identities.values())) != len(identities):
        rendered = ", ".join(f"${name}={actor}" for name, actor in identities.items())
        problems.append(
            "configured GitHub credentials resolve to the same actor; "
            f"self-review cannot satisfy branch protection ({rendered})"
        )
    return problems


# --- the fast local gate -----------------------------------------------------


def fast_gate(worktree: pathlib.Path, tests: list[str], config: dict) -> dict:
    """Cheap pre-filter, cheapest step first. Not the definition of green.

    Returns {passed, step, output}. `step` names what failed so the builder
    gets told which thing broke rather than that something did.
    """
    python = interpreter(config)
    ruff = ruff_cmd(python)
    steps: list[tuple[str, list[str]]] = []
    if ruff:
        steps.append(("ruff", [*ruff, "check", "."]))
    steps += [
        ("money-float lint (ED-8)", [python, "tools/ci/money_float_lint.py"]),
        ("banned-call check (AR-2)", [python, "tools/ci/banned_calls.py"]),
    ]
    pytest_paths = [t for t in tests if t.endswith(".py")]
    vitest_paths = [t for t in tests if t.endswith((".tsx", ".ts"))]
    if pytest_paths:
        steps.append(("acceptance (pytest)", [python, "-m", "pytest", "-q", "-p",
                                              "no:cacheprovider", *pytest_paths]))
    if vitest_paths:
        steps.append(("acceptance (vitest)",
                      ["npm", "exec", "--prefix", "console", "--", "vitest", "run",
                       "--config", "../tests/acceptance/console/vitest.config.ts",
                       *vitest_paths]))

    for name, cmd in steps:
        code, out = _run(cmd, worktree, timeout=config["breakers"]["fast_gate_seconds"])
        if code != 0:
            return {"passed": False, "step": name, "output": out[-8000:]}
    return {"passed": True, "step": "", "output": ""}
