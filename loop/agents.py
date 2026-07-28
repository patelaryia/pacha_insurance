#!/usr/bin/env python3
"""Invoking the builder and the reviewer, and validating what they return.

Agents return a JSON document at a path the controller chose. They do not
return a status, and nothing they write to a packet file has any effect —
the controller owns every transition. An agent's JSON is a *recommendation*
plus evidence; the controller decides what it means.

The builder writes its result inside its own worktree
(`<worktree>/.loop/result.json`) and the controller reads it from there.
That is deliberate: the builder's filesystem is the only one it can write
to, and the previous design's escalation channel was dead because the
controller read a different copy of the file on a different filesystem.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time

RESULT_REL = ".loop/result.json"

BUILDER_OUTCOMES = frozenset({"built", "escalated", "gave_up"})
REVIEW_VERDICTS = frozenset({"approve", "rework", "escalate"})

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"enum": sorted(REVIEW_VERDICTS)},
        "blocking": {"type": "array", "items": {"type": "string"}},
        "non_blocking": {"type": "array", "items": {"type": "string"}},
        "judgement_calls": {"type": "array", "items": {"type": "string"}},
        "escalation": {"type": ["string", "null"]},
    },
    "required": [
        "verdict",
        "blocking",
        "non_blocking",
        "judgement_calls",
        "escalation",
    ],
}


class AgentError(Exception):
    """An agent returned something the controller cannot act on."""


# --- result schemas ----------------------------------------------------------
#
# Hand-checked rather than jsonschema: the shapes are small, the errors need
# to name the offending field in prose an operator can act on, and adding a
# dependency to validate two objects is the kind of machinery the code
# standards reject.


def validate_builder_result(raw: dict) -> dict:
    if raw.get("outcome") not in BUILDER_OUTCOMES:
        raise AgentError(
            f"builder result outcome={raw.get('outcome')!r}, expected one of "
            f"{sorted(BUILDER_OUTCOMES)}"
        )
    if raw["outcome"] == "escalated":
        escalation = raw.get("escalation") or {}
        missing = [k for k in ("reason", "spec_clause") if not escalation.get(k)]
        if missing:
            raise AgentError(
                f"builder escalated without {', '.join(missing)}. An escalation without a "
                f"spec citation is an opinion, and the controller does not act on opinions."
            )
    return {
        "outcome": raw["outcome"],
        "summary": str(raw.get("summary", ""))[:2000],
        "escalation": raw.get("escalation"),
    }


def validate_review_verdict(raw: dict) -> dict:
    verdict = raw.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        raise AgentError(
            f"review verdict={verdict!r}, expected one of {sorted(REVIEW_VERDICTS)}"
        )
    blocking = raw.get("blocking") or []
    if verdict == "rework" and not blocking:
        raise AgentError(
            "verdict=rework with no blocking findings — nothing for the builder to fix"
        )
    if verdict == "approve" and blocking:
        raise AgentError("verdict=approve with blocking findings — pick one")
    if verdict == "escalate" and not raw.get("escalation"):
        raise AgentError("verdict=escalate with no written escalation")
    return {
        "verdict": verdict,
        "blocking": [str(b) for b in blocking],
        "non_blocking": [str(b) for b in (raw.get("non_blocking") or [])],
        "judgement_calls": [str(b) for b in (raw.get("judgement_calls") or [])],
        "escalation": raw.get("escalation"),
    }


def read_result(worktree: pathlib.Path) -> dict | None:
    path = worktree / RESULT_REL
    if not path.exists():
        return None
    try:
        return validate_builder_result(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} is not valid JSON: {exc}") from None


def clear_result(worktree: pathlib.Path) -> None:
    """Remove the previous attempt's result before starting a new one, so a
    stale `escalated` from attempt 1 cannot be read as attempt 2's answer."""
    path = worktree / RESULT_REL
    if path.exists():
        path.unlink()


# --- invocation --------------------------------------------------------------


def _agent_env(
    config: dict,
    *,
    credential_sandbox: pathlib.Path | None = None,
) -> dict[str, str]:
    """A deliberately small environment for untrusted agent processes.

    In particular, GitHub builder/reviewer tokens, PACHA runtime secrets,
    database URLs and cloud credentials never cross into Codex or Claude.
    Model credentials are retained only because the agent CLI may require
    them; keychain-backed authentication remains preferable.
    """
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    github = config.get("github") or {}
    for name in (
        github.get("builder_token_env"),
        github.get("reviewer_token_env"),
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        if name:
            env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/usr/bin/false"
    # HOME must remain available for the model CLIs' own authentication, but
    # gh and git must not inherit controller credentials from files under it.
    if credential_sandbox is not None:
        env["GH_CONFIG_DIR"] = str(credential_sandbox)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
    return env


def _spawn(
    cmd: list[str],
    *,
    stdin_path: pathlib.Path,
    run_dir: pathlib.Path,
    timeout: int,
    config: dict,
    stdout_path: pathlib.Path | None = None,
) -> dict:
    """Run an agent under a hard wall clock and classify how it ended.

    Returns outcome in: exited (with code) | timeout | spawn_failed. The
    previous design threw the exit code away with `|| true`, so its timeout
    was produced and never consumed.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    credential_sandbox = run_dir / "no-controller-credentials"
    credential_sandbox.mkdir(exist_ok=True)
    started = time.time()
    with open(stdin_path) as stdin, \
         open(stdout_path or run_dir / "events.jsonl", "w") as out, \
         open(run_dir / "stderr.log", "w") as err:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=stdin,
                stdout=out,
                stderr=err,
                env=_agent_env(
                    config,
                    credential_sandbox=credential_sandbox,
                ),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return {"outcome": "spawn_failed", "code": None, "wall": 0, "detail": str(exc)}
        try:
            code = proc.wait(timeout=timeout)
            return {"outcome": "exited", "code": code,
                    "wall": int(time.time() - started), "detail": ""}
        except subprocess.TimeoutExpired:
            # Kill the whole process group. Terminating only the CLI can leave
            # tests or shell children modifying the worktree while the
            # controller starts validating it.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            return {"outcome": "timeout", "code": None,
                    "wall": int(time.time() - started),
                    "detail": f"killed after {timeout}s"}


def run_builder(config: dict, worktree: pathlib.Path, run_dir: pathlib.Path,
                prompt: str, timeout: int) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)
    cmd = [
        config["builder"]["command"], "exec", "--json",
        "--output-last-message", str(run_dir / "last-message.txt"),
        "--cd", str(worktree),
        "--sandbox", config["builder"]["sandbox"],
        "--skip-git-repo-check",
        "-",
    ]
    model = config["builder"].get("model")
    if model:
        cmd[2:2] = ["--model", model]
    result = _spawn(
        cmd,
        stdin_path=run_dir / "prompt.txt",
        run_dir=run_dir,
        timeout=timeout,
        config=config,
    )
    result["tokens"] = parse_tokens(run_dir / "events.jsonl")
    return result


def run_reviewer(config: dict, repo: pathlib.Path, run_dir: pathlib.Path,
                 prompt: str, timeout: int) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)
    output_path = run_dir / "reviewer-output.json"
    cmd = [
        config["reviewer"]["command"],
        "-p",
        "--permission-mode",
        config["reviewer"]["permission_mode"],
        "--no-session-persistence",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(REVIEW_SCHEMA, separators=(",", ":")),
        "--allowedTools",
        (
            "Read,Glob,Grep,Bash(git diff *),Bash(git log *),"
            "Bash(git show *),Bash(gh pr view *),Bash(gh pr diff *)"
        ),
    ]
    model = config["reviewer"].get("model")
    if model:
        cmd += ["--model", model]
    result = _spawn(
        cmd,
        stdin_path=run_dir / "prompt.txt",
        run_dir=run_dir,
        timeout=timeout,
        config=config,
        stdout_path=output_path,
    )
    if result["outcome"] == "exited" and result["code"] == 0:
        try:
            result["structured"] = parse_reviewer_output(output_path)
        except AgentError as exc:
            result["structured_error"] = str(exc)
    return result


def parse_reviewer_output(path: pathlib.Path) -> dict:
    """Extract Claude's JSON-schema result without granting it file writes."""
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"reviewer output is not valid JSON: {exc}") from None
    if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
        return validate_review_verdict(envelope["structured_output"])
    if isinstance(envelope, dict) and "verdict" in envelope:
        return validate_review_verdict(envelope)
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if isinstance(result, str):
        try:
            return validate_review_verdict(json.loads(result))
        except json.JSONDecodeError:
            pass
    raise AgentError("Claude returned no structured review verdict")


def parse_tokens(events: pathlib.Path) -> int | None:
    """Best effort. `codex exec --json` emits JSONL, but token accounting is
    not a documented part of that contract. Returns None rather than a
    number the budget would then pretend to trust — a partial token count
    that looks like a budget is worse than no budget."""
    if not events.exists():
        return None
    total, found = 0, False
    for line in events.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") or (event.get("msg") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        for key in ("total_tokens", "total_token_usage"):
            value = usage.get(key)
            if isinstance(value, int):
                total, found = max(total, value), True
            elif isinstance(value, dict) and isinstance(value.get("total_tokens"), int):
                total, found = max(total, value["total_tokens"]), True
    return total if found else None


# --- prompts -----------------------------------------------------------------


def builder_prompt(packet, packet_rel: str, oracle: list[str], base_sha: str,
                   feedback: str) -> str:
    """The builder's entire input. One packet, the repo, the skills, and —
    on a retry or rework — the concrete output of what failed last time.

    It does not receive other packets, other PRDs, or any prior transcript.
    It does receive its own last failure, because three identical attempts
    cost three times as much as one and learn nothing.
    """
    frozen = "\n".join(f"    {p}" for p in oracle) or "    (none)"
    prior = f"\n\n{feedback.strip()}\n" if feedback.strip() else ""
    return f"""You are the builder for the Pacha claims platform.

Read, in this order, and treat them as binding:

  1. AGENTS.md                                - your brief
  2. .claude/skills/code-standards/SKILL.md   - how code must be written
  3. {packet_rel}
     the packet you are building
  4. the PRD named in that packet's prd_ref   - the spec behind it

Your branch is already checked out, based on {base_sha[:12]}.

STOPPING CONDITION. All of these green, and nothing else counts:

    the acceptance tests the packet names
    ruff check .
    tools/ci/money_float_lint.py
    tools/ci/banned_calls.py
    the console build, if you changed console/

THE DEFINITION OF DONE IS FROZEN. These exact files are pinned by content
hash and you may not modify them:

{frozen}

You MUST still write the unit and integration tests ED-7 requires. New test
files are yours to create — only the pinned files above are frozen. The
controller verifies those hashes in this worktree after you exit and will
refuse to open a PR if one moved, so editing them does not work; it only
wastes an attempt.

If you believe a pinned test contradicts the spec, do not edit it. Stop and
write the escalation form below instead. A human decides.

WHEN YOU ARE DONE, write {RESULT_REL} in this worktree:

    {{"outcome": "built", "summary": "one paragraph on what you changed"}}

or, if you are escalating:

    {{"outcome": "escalated",
      "summary": "...",
      "escalation": {{"reason": "what is wrong",
                     "spec_clause": "ED-/AR-/PRD- clause it contradicts",
                     "test": "path::test_name if a pinned test is the problem",
                     "should_assert": "what the spec says it should assert"}}}}

or, if you cannot finish and are not escalating:

    {{"outcome": "gave_up", "summary": "what you tried and where you stopped"}}

That file is the ONLY channel the controller reads. Editing the packet
file's front matter changes nothing — the controller owns status.

COMMIT your work on the current branch. A worktree with uncommitted changes
is treated as a failed attempt. Do not push. Do not open a PR.

If the packet is underdetermined, follow AGENTS.md section 5: ship the
narrowest safe behaviour and append an open-items register entry. Never
pick a value the spec does not give you.{prior}"""


def reviewer_prompt(packet_id: str, packet_rel: str, pr: int) -> str:
    return f"""You are the CTO reviewer for Pacha packet {packet_id}.

The packet is {packet_rel}. Its PR is #{pr}. CI is green — do not re-run it.

Follow .claude/skills/review-criteria/SKILL.md exactly, including the rule
that you must not read loop/runs/, the builder's commit messages beyond the
diff, or any transcript.

Return only the structured verdict requested by the JSON schema:

    {{"verdict": "approve" | "rework" | "escalate",
      "blocking": ["file:line - finding, spec clause"],
      "non_blocking": ["..."],
      "judgement_calls": ["required on approve if you were genuinely unsure"],
      "escalation": "required prose on escalate, else null"}}

You are read-only. Do not write any file, set any status, approve, or merge.
The controller decides what your verdict means.
"""
