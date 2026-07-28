"""End-to-end tests for the controller state machine.

Not collected by the project suite (`pyproject.toml: testpaths = ["tests"]`).
Run explicitly:

    python3 -m pytest loop/test_controller.py -q

Every test here exercises a path the previous design got wrong. The previous
design's tests covered the circuit breakers — the part that was easy to test
— and its dry run walked a happy path over an empty board. Every one of the
ten defects the owner found lived in a path neither touched. So the rule for
this file is: a transition that is not tested here does not exist.

The builder, the reviewer and GitHub are faked. What is real is the
controller's decision-making, the ledger, the git worktree, and the oracle.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import agents  # noqa: E402
import audit  # noqa: E402
import controller as C  # noqa: E402
import forge  # noqa: E402
import gates  # noqa: E402
import packets as P  # noqa: E402
import store  # noqa: E402

PACKET_BODY = """
# {pid}

Body. What to build, constraints, non-goals, acceptance.
"""


def git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A real git repo, a real board, a real ledger, fake agents and forge."""
    repo = tmp_path / "repo"
    (repo / "docs" / "packets").mkdir(parents=True)
    (repo / "tests" / "acceptance").mkdir(parents=True)
    (repo / "loop").mkdir()
    (repo / "tools" / "ci").mkdir(parents=True)

    oracle_test = repo / "tests" / "acceptance" / "test_thing.py"
    oracle_test.write_text("def test_thing():\n    assert True\n")
    (repo / "tools" / "ci" / "money_float_lint.py").write_text("")
    (repo / "tools" / "ci" / "banned_calls.py").write_text("")
    (repo / "loop" / "blast-radius.yml").write_text(
        (pathlib.Path(__file__).parent / "blast-radius.yml").read_text())

    git(["init", "-q", "-b", "main"], repo)
    git(["config", "user.email", "loop@test"], repo)
    git(["config", "user.name", "loop"], repo)
    git(["add", "-A"], repo)
    git(["commit", "-qm", "base"], repo)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()

    monkeypatch.setattr(P, "REPO", repo)
    monkeypatch.setattr(C, "REPO", repo)
    monkeypatch.setattr(C, "PAUSE_FILE", repo / "loop" / "PAUSED")
    monkeypatch.setattr(forge, "REPO", repo)
    monkeypatch.setattr(P, "ORACLE_LOCK", repo / "loop" / "oracle.lock")

    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    config["board_dir"] = "docs/packets"
    config["breakers"]["max_attempts"] = 2
    config["breakers"]["max_rework_cycles"] = 2
    config["audit"]["enabled"] = False
    config["worker"].update(
        {
            "poll_initial_seconds": 0,
            "retry_initial_seconds": 0,
            "preflight_initial_seconds": 0,
            "poll_max_seconds": 0,
        }
    )

    # Preflight and the fast gate are the machine, not the state machine.
    # They have their own tests; here they are made to pass so the
    # transitions under test are the thing that can fail.
    monkeypatch.setattr(gates, "preflight", lambda *a, **k: [])
    monkeypatch.setattr(gates, "fast_gate",
                        lambda *a, **k: {"passed": True, "step": "", "output": ""})
    monkeypatch.setattr(forge, "resolve_base", lambda *a, **k: base_sha)
    monkeypatch.setattr(forge, "verify_repo_identity", lambda *a, **k: None)
    monkeypatch.setattr(forge, "ensure_review_worktree", lambda *a, **k: None)
    monkeypatch.setattr(forge, "remove_worktree", lambda *a, **k: None)

    conn = store.open_db(repo / "loop" / "state.db")
    store.init(conn)

    class World:
        def __init__(self):
            self.repo = repo
            self.conn = conn
            self.config = config
            self.base_sha = base_sha
            self.pushed = []
            self.prs = {}
            self.ci = {}
            self.pr_states = {}
            self.heads = {}
            self.merged = []
            self.next_pr = 100

        def add_packet(
            self,
            pid,
            *,
            depends_on=(),
            blast=False,
            tests=("tests/acceptance/test_thing.py",),
            status="queued",
        ):
            meta = {
                "id": pid, "prd_ref": "docs/PRD-00.md", "title": f"{pid} title",
                "depends_on": list(depends_on), "branch": f"loop/{pid.lower()}",
                "blast_radius": blast, "acceptance_tests": list(tests),
                "status": status, "pr": None, "attempts": 0, "reason": None,
            }
            path = repo / "docs" / "packets" / f"{pid}.md"
            path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n"
                            + PACKET_BODY.format(pid=pid))
            return path

        def pin_oracle(self):
            board = P.load_board(repo / "docs" / "packets")
            P.write_oracle(P.build_oracle(board, repo))

        def controller(self, *, synced=False, **kwargs):
            ctl = C.Controller(config, conn, board_dir="docs/packets", **kwargs)
            if synced:
                ctl.sync_board()
            return ctl

        def row(self, pid):
            return store.get(conn, pid)

        def attempts(self, pid):
            return conn.execute(
                "SELECT * FROM attempts WHERE packet_id=? ORDER BY id", (pid,)).fetchall()

        def events(self, pid, kind=None):
            rows = store.events(conn, pid)
            return [r for r in rows if kind is None or r["kind"] == kind]

    w = World()

    # --- fake forge ---------------------------------------------------------
    def fake_push(worktree, branch, **kw):
        w.pushed.append(branch)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(worktree),
                              capture_output=True, text=True).stdout.strip()
        w.heads[branch] = head
        return head

    def fake_ensure_pr(worktree, branch, title, body, **kw):
        if branch not in w.prs:
            w.prs[branch] = w.next_pr
            w.pr_states[w.next_pr] = "OPEN"
            w.next_pr += 1
        return w.prs[branch]

    def fake_ci_state(repo_, pr, required, **kw):
        row = next(
            (item for item in store.all_packets(w.conn) if item["pr_number"] == pr),
            None,
        )
        state = dict(
            w.ci.get(
                pr,
                {
                    "verdict": "pending",
                    "failing": [],
                    "unreported": required,
                    "runs": {},
                },
            )
        )
        branch = row["branch"] if row else None
        state.setdefault("head_sha", row["head_sha"] if row else None)
        state.setdefault("head_ref", branch)
        state.setdefault("base_ref", "main")
        return state

    def fake_pr_state(repo_, pr, **kw):
        row = next(
            (item for item in store.all_packets(w.conn) if item["pr_number"] == pr),
            None,
        )
        branch = row["branch"] if row else next(
            (name for name, number in w.prs.items() if number == pr),
            None,
        )
        return {"state": w.pr_states.get(pr, "OPEN"), "merged_at": None,
                "merge_commit": "deadbeef",
                "head_sha": w.heads.get(
                    branch,
                    row["head_sha"] if row else None,
                ),
                "head_ref": branch,
                "base_ref": "main"}

    def fake_merge(repo_, pr, **kw):
        w.merged.append(pr)
        w.pr_states[pr] = "MERGED"
        return "cafebabe"

    monkeypatch.setattr(forge, "push", fake_push)
    monkeypatch.setattr(forge, "ensure_pr", fake_ensure_pr)
    monkeypatch.setattr(forge, "ci_state", fake_ci_state)
    monkeypatch.setattr(forge, "pr_state", fake_pr_state)
    monkeypatch.setattr(forge, "merge", fake_merge)
    monkeypatch.setattr(forge, "approve", lambda *a, **k: None)
    monkeypatch.setattr(forge, "failing_logs", lambda *a, **k: "FAILED: test_x")

    yield w
    conn.close()


def fake_builder(behaviour):
    """Return a run_builder stand-in. `behaviour(worktree)` does whatever the
    scenario needs — commit, escalate, leave the tree dirty, nothing."""
    def run(config, worktree, run_dir, prompt, timeout):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt)
        return behaviour(worktree) or {"outcome": "exited", "code": 0, "wall": 1,
                                       "detail": "", "tokens": None}
    return run


def commits(worktree, *, message="work", result="built", touch="platform/thing.py"):
    path = worktree / touch
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {time.time()}\n")
    (worktree / ".loop").mkdir(exist_ok=True)
    (worktree / ".loop" / "result.json").write_text(
        json.dumps({"outcome": result, "summary": message}))
    git(["add", "-A", ":!.loop"], worktree)
    git(["commit", "-qm", message], worktree)


# --- the happy path ----------------------------------------------------------


def test_green_build_opens_a_pr_and_waits_for_ci(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))

    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "awaiting_ci"
    assert row["pr_number"] == 100
    assert world.pushed == ["loop/packet-01"]
    assert world.attempts("PACKET-01")[0]["outcome"] == "published"


def test_ci_green_moves_to_review_then_approval_reaches_merge_ready(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}

    def reviewer(config, repo, run_dir, prompt, timeout):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "verdict.json").write_text(json.dumps(
            {"verdict": "approve", "blocking": [], "judgement_calls": ["boundary at 50.0%"]}))
        return {"outcome": "exited", "code": 0, "wall": 1, "detail": ""}

    monkeypatch.setattr(agents, "run_reviewer", reviewer)
    world.controller().tick()

    assert world.row("PACKET-01")["status"] == "merge_ready"
    verdicts = world.events("PACKET-01", "review_verdict")
    assert json.loads(verdicts[-1]["payload"])["judgement_calls"] == ["boundary at 50.0%"]


# --- durable lifecycle worker -----------------------------------------------


def test_worker_continues_builder_ci_review_rework_builder_to_completion(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    world.config["github"]["auto_merge"] = True
    world.config["github"]["builder_token_env"] = "FAKE_BUILDER"
    world.config["github"]["reviewer_token_env"] = "FAKE_REVIEWER"
    world.ci[100] = {
        "verdict": "green",
        "failing": [],
        "unreported": [],
        "runs": {},
    }

    prompts = []

    def builder(config, worktree, run_dir, prompt, timeout):
        prompts.append(prompt)
        run_dir.mkdir(parents=True, exist_ok=True)
        commits(worktree, message=f"build {len(prompts)}")
        return {
            "outcome": "exited",
            "code": 0,
            "wall": 1,
            "detail": "",
            "tokens": None,
        }

    verdicts = iter(
        [
            {
                "verdict": "rework",
                "blocking": ["service.py:14 - preserve the PRD boundary"],
            },
            {"verdict": "approve", "blocking": []},
        ]
    )

    def reviewer(config, repo, run_dir, prompt, timeout):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "verdict.json").write_text(json.dumps(next(verdicts)))
        return {"outcome": "exited", "code": 0, "wall": 1, "detail": ""}

    monkeypatch.setattr(agents, "run_builder", builder)
    monkeypatch.setattr(agents, "run_reviewer", reviewer)

    ctl = world.controller(quiet=True)
    lifecycle = ctl.start_lifecycle("PACKET-01")
    outcome = ctl.run_worker(
        "PACKET-01",
        sleeper=lambda _seconds: None,
        max_cycles=6,
    )

    assert outcome == "completed"
    assert store.latest_lifecycle(world.conn, "PACKET-01")["id"] == lifecycle["id"]
    assert world.row("PACKET-01")["status"] == "merged"
    assert len(prompts) == 2
    assert "preserve the PRD boundary" in prompts[1]
    assert world.pushed == ["loop/packet-01", "loop/packet-01"]
    assert [row["kind"] for row in store.notifications(world.conn, "PACKET-01")] == [
        "started",
        "rework_needed",
        "completed",
    ]


def test_active_lifecycle_and_unchanged_preflight_notification_are_deduplicated(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(
        gates,
        "preflight",
        lambda *args, **kwargs: ["GitHub authentication is unavailable"],
    )

    ctl = world.controller(quiet=True)
    first = ctl.start_lifecycle("PACKET-01")
    second = ctl.start_lifecycle("PACKET-01")
    ctl.worker_cycle("PACKET-01")
    ctl.worker_cycle("PACKET-01")

    assert first["id"] == second["id"]
    assert world.conn.execute("SELECT COUNT(*) FROM lifecycles").fetchone()[0] == 1
    updates = store.notifications(world.conn, "PACKET-01")
    assert [row["kind"] for row in updates] == ["started", "blocked"]
    assert "GitHub authentication" in updates[-1]["message"]


def test_owner_activation_is_seeded_before_builder_work(world, monkeypatch):
    """The red owner contract travels in the final implementation PR."""
    contract = world.repo / "tests" / "acceptance" / "test_new_contract.py"
    contract.write_text("def test_owner_contract():\n    assert True\n")
    packet = world.add_packet(
        "PACKET-01",
        tests=("tests/acceptance/test_new_contract.py",),
    )
    world.pin_oracle()
    git(
        [
            "add",
            str(packet.relative_to(world.repo)),
            str(contract.relative_to(world.repo)),
            "loop/oracle.lock",
        ],
        world.repo,
    )
    git(["commit", "-qm", "owner activates packet"], world.repo)
    activation_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(world.repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    observed = {}

    def builder(worktree):
        observed["packet"] = (
            worktree / "docs" / "packets" / "PACKET-01.md"
        ).read_text()
        observed["contract"] = (
            worktree / "tests" / "acceptance" / "test_new_contract.py"
        ).read_text()
        commits(worktree, message="implement owner contract")

    monkeypatch.setattr(agents, "run_builder", fake_builder(builder))

    ctl = world.controller(quiet=True)
    lifecycle = ctl.start_lifecycle("PACKET-01")
    result = ctl.worker_cycle("PACKET-01")

    row = world.row("PACKET-01")
    assert result == "ci"
    assert row["status"] == "awaiting_ci"
    assert lifecycle["base_sha"] == world.base_sha
    assert lifecycle["activation_sha"] == activation_sha
    assert row["activation_head_sha"]
    assert "# PACKET-01" in observed["packet"]
    assert "test_owner_contract" in observed["contract"]
    worktree = ctl.worktree_for("PACKET-01")
    assert forge.changed_paths(worktree, row["activation_head_sha"]) == [
        "platform/thing.py"
    ]
    assert set(forge.changed_paths(worktree, world.base_sha)) == {
        "docs/packets/PACKET-01.md",
        "loop/oracle.lock",
        "platform/thing.py",
        "tests/acceptance/test_new_contract.py",
    }


def test_activation_refuses_product_changes_before_lifecycle_start(world):
    packet = world.add_packet("PACKET-01")
    world.pin_oracle()
    product = world.repo / "platform" / "smuggled.py"
    product.parent.mkdir()
    product.write_text("# not an owner activation input\n")
    git(
        [
            "add",
            str(packet.relative_to(world.repo)),
            "loop/oracle.lock",
            str(product.relative_to(world.repo)),
        ],
        world.repo,
    )
    git(["commit", "-qm", "mixed activation"], world.repo)

    with pytest.raises(P.SpecError, match="outside the owner packet contract"):
        world.controller().start_lifecycle("PACKET-01")

    assert store.active_lifecycle(world.conn, "PACKET-01") is None


def test_worker_recovers_same_lifecycle_after_transient_preflight_failure(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    checks = iter(
        [
            ["network cannot reach GitHub"],
            ["network cannot reach GitHub"],
            [],
        ]
    )
    monkeypatch.setattr(gates, "preflight", lambda *args, **kwargs: next(checks))
    builds = []

    def builder(config, worktree, run_dir, prompt, timeout):
        builds.append(prompt)
        run_dir.mkdir(parents=True, exist_ok=True)
        commits(worktree)
        return {
            "outcome": "exited",
            "code": 0,
            "wall": 1,
            "detail": "",
            "tokens": None,
        }

    monkeypatch.setattr(agents, "run_builder", builder)
    ctl = world.controller(quiet=True)
    lifecycle = ctl.start_lifecycle("PACKET-01")

    outcome = ctl.run_worker(
        "PACKET-01",
        sleeper=lambda _seconds: None,
        max_cycles=3,
    )

    assert outcome == "active"
    assert store.active_lifecycle(world.conn, "PACKET-01")["id"] == lifecycle["id"]
    assert world.row("PACKET-01")["status"] == "awaiting_ci"
    assert len(builds) == 1
    updates = store.notifications(world.conn, "PACKET-01")
    assert [row["kind"] for row in updates] == ["started", "blocked", "started"]
    assert "preflight recovered" in updates[-1]["message"]


def test_worker_completes_only_after_the_required_owner_merge_gate(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    world.ci[100] = {
        "verdict": "green",
        "failing": [],
        "unreported": [],
        "runs": {},
    }
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(lambda worktree: commits(worktree)),
    )
    monkeypatch.setattr(
        agents,
        "run_reviewer",
        lambda config, repo, run_dir, prompt, timeout: (
            run_dir.mkdir(parents=True, exist_ok=True),
            (run_dir / "verdict.json").write_text(
                json.dumps({"verdict": "approve", "blocking": []})
            ),
            {"outcome": "exited", "code": 0, "wall": 1, "detail": ""},
        )[-1],
    )
    ctl = world.controller(quiet=True)
    lifecycle = ctl.start_lifecycle("PACKET-01")

    assert ctl.run_worker(
        "PACKET-01",
        sleeper=lambda _seconds: None,
        max_cycles=2,
    ) == "active"
    assert world.row("PACKET-01")["status"] == "merge_ready"
    assert store.active_lifecycle(world.conn, "PACKET-01")["id"] == lifecycle["id"]
    assert [row["kind"] for row in store.notifications(world.conn, "PACKET-01")] == [
        "started",
        "blocked",
    ]

    world.pr_states[100] = "MERGED"
    assert ctl.run_worker(
        "PACKET-01",
        sleeper=lambda _seconds: None,
        max_cycles=1,
    ) == "completed"
    assert store.latest_lifecycle(world.conn, "PACKET-01")["id"] == lifecycle["id"]
    assert [row["kind"] for row in store.notifications(world.conn, "PACKET-01")] == [
        "started",
        "blocked",
        "completed",
    ]


# --- 1. build failure --------------------------------------------------------


def test_failed_gate_retries_then_blocks_at_the_cap(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    monkeypatch.setattr(gates, "fast_gate", lambda *a, **k: {
        "passed": False, "step": "acceptance (pytest)", "output": "1 failed"})

    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "queued"     # retryable

    world.controller().tick()
    row = world.row("PACKET-01")
    assert row["status"] == "blocked"
    assert "cap is 2" in row["reason"]
    assert [a["outcome"] for a in world.attempts("PACKET-01")] == ["gate_failed", "gate_failed"]


def test_failure_output_reaches_the_next_attempt(world, monkeypatch):
    """Three identical attempts cost three times as much as one and learn
    nothing. The previous design passed no failure context forward."""
    world.add_packet("PACKET-01")
    world.pin_oracle()
    seen = []

    def run(config, worktree, run_dir, prompt, timeout):
        seen.append(prompt)
        run_dir.mkdir(parents=True, exist_ok=True)
        commits(worktree, message=f"try{len(seen)}")
        return {"outcome": "exited", "code": 0, "wall": 1, "detail": "", "tokens": None}

    monkeypatch.setattr(agents, "run_builder", run)
    monkeypatch.setattr(gates, "fast_gate", lambda *a, **k: {
        "passed": False, "step": "acceptance (pytest)", "output": "assert 1 == 2"})

    world.controller().tick()
    world.controller().tick()

    assert "assert 1 == 2" not in seen[0]
    assert "assert 1 == 2" in seen[1]
    assert "YOUR PREVIOUS ATTEMPT FAILED" in seen[1]


# --- 2. timeout --------------------------------------------------------------


def test_timeout_is_classified_not_swallowed(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", lambda *a, **k: {
        "outcome": "timeout", "code": None, "wall": 5400, "detail": "killed after 5400s",
        "tokens": None})

    world.controller().tick()

    assert world.attempts("PACKET-01")[0]["outcome"] == "timeout"
    assert world.row("PACKET-01")["status"] == "queued"
    assert world.pushed == []


# --- 3. rework ---------------------------------------------------------------


def test_rework_is_dispatchable_and_does_not_deadlock_the_loop(world, monkeypatch):
    """The previous design set status=rework and then only ever selected
    `queued`, while counting `rework` as an occupied slot. At concurrency 1
    the first requested rework stopped the entire loop forever.

    Here rework is dispatchable, so one tick carries the packet from green
    CI through the reviewer's findings and straight back into the builder,
    reusing the same branch and the same PR.
    """
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}

    verdicts = iter(["rework", "approve"])

    def reviewer(config, repo, run_dir, prompt, timeout):
        run_dir.mkdir(parents=True, exist_ok=True)
        verdict = next(verdicts, "approve")
        body = ({"verdict": "rework",
                 "blocking": ["calcs.py:12 - boundary. code-standards 4.2"]}
                if verdict == "rework" else {"verdict": "approve", "blocking": []})
        (run_dir / "verdict.json").write_text(json.dumps(body))
        return {"outcome": "exited", "code": 0, "wall": 1, "detail": ""}

    prompts = []
    monkeypatch.setattr(agents, "run_reviewer", reviewer)
    monkeypatch.setattr(agents, "run_builder", lambda c, wt, rd, prompt, tmo: (
        prompts.append(prompt), commits(wt, message="fix"),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": "", "tokens": None})[-1])

    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "awaiting_ci"          # went back to the builder, not stuck
    assert row["pr_number"] == 100                 # same PR reused
    assert row["rework_cycles"] == 1
    assert world.pushed == ["loop/packet-01", "loop/packet-01"]   # same branch
    assert "calcs.py:12" in prompts[0]             # the findings reached it


def test_a_second_tick_after_rework_still_progresses(world, monkeypatch):
    """Regression guard: a rework must not leave the loop unable to select
    anything on the following tick either."""
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "red", "failing": ["console"], "unreported": [], "runs": {}}
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, tmo: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps({"verdict": "approve", "blocking": []})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])
    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "merge_ready"


def test_reviewer_findings_reach_the_builder(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, tmo: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps(
            {"verdict": "rework", "blocking": ["service.py:14 - swallowed exception"]})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])

    seen = []
    monkeypatch.setattr(agents, "run_builder", lambda c, wt, rd, prompt, tmo: (
        seen.append(prompt), commits(wt, message="fix"),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": "", "tokens": None})[-1])
    world.controller().tick()

    assert "swallowed exception" in seen[0]
    assert "THE REVIEWER RETURNED BLOCKING FINDINGS" in seen[0]


def test_rework_cycles_are_capped(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    world.config["breakers"]["max_attempts"] = 10
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, t: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps(
            {"verdict": "rework", "blocking": ["still wrong"]})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])

    for _ in range(8):
        world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
        world.controller().tick()
        if world.row("PACKET-01")["status"] == "blocked":
            break

    row = world.row("PACKET-01")
    assert row["status"] == "blocked"
    assert "rework cycles" in row["reason"]


# --- 4. red CI ---------------------------------------------------------------


def test_red_ci_becomes_rework_with_the_failing_output(world, monkeypatch):
    """The previous design told the reviewer to stop on red CI and then
    changed nothing, so the packet sat at `review` forever."""
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    world.ci[100] = {"verdict": "red", "failing": ["tests (PostgreSQL required)"],
                     "unreported": [], "runs": {}}
    world.controller().tick()

    assert world.row("PACKET-01")["status"] in ("rework", "awaiting_ci")
    feedback = [json.loads(e["payload"])["text"]
                for e in world.events("PACKET-01", "feedback")]
    assert any("CI IS RED" in f and "FAILED: test_x" in f for f in feedback)


def test_unreported_required_check_is_pending_not_green(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    world.ci[100] = {"verdict": "pending", "failing": [],
                     "unreported": ["tests (PostgreSQL required)"], "runs": {}}
    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "awaiting_ci"


# --- 5. publish failure ------------------------------------------------------


def test_push_failure_does_not_produce_a_reviewable_packet(world, monkeypatch):
    """The previous design ignored the push result and stored the text of a
    failed `gh pr create` as if it were a PR URL, then moved to review."""
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    monkeypatch.setattr(forge, "push", lambda *a, **k: (_ for _ in ()).throw(
        forge.ForgeError("remote rejected")))

    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "queued"
    assert row["pr_number"] is None
    assert world.attempts("PACKET-01")[0]["outcome"] == "publish_failed"


def test_dirty_worktree_is_a_failed_attempt(world, monkeypatch):
    """Uncommitted work is invisible to a diff against the base, which is
    exactly how a false-green PR got published before."""
    def leave_dirty(worktree):
        commits(worktree)
        (worktree / "platform" / "uncommitted.py").write_text("x = 1\n")

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(leave_dirty))

    world.controller().tick()

    assert world.attempts("PACKET-01")[0]["outcome"] == "dirty_worktree"
    assert world.pushed == []


def test_local_gate_cannot_dirty_worktree_after_initial_validation(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(lambda worktree: commits(worktree)),
    )

    def dirty_gate(worktree, *args, **kwargs):
        (worktree / "platform" / "gate-output.txt").write_text("generated\n")
        return {"passed": True, "step": "", "output": ""}

    monkeypatch.setattr(gates, "fast_gate", dirty_gate)

    world.controller().tick()

    assert world.attempts("PACKET-01")[0]["outcome"] == "gate_dirtied_worktree"
    assert world.pushed == []


def test_zero_commits_is_a_failed_attempt(world, monkeypatch):
    def do_nothing(worktree):
        (worktree / ".loop").mkdir(exist_ok=True)
        (worktree / ".loop" / "result.json").write_text(
            json.dumps({"outcome": "built", "summary": "I thought about it"}))

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(do_nothing))

    world.controller().tick()

    assert world.attempts("PACKET-01")[0]["outcome"] == "no_commits"
    assert world.pushed == []


# --- 6. crash recovery -------------------------------------------------------


def test_expired_lease_is_reclaimed_and_the_packet_runs_again(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    with store.write(world.conn):
        store.upsert_spec(world.conn, "PACKET-01", "loop/packet-01")
        store.set_status(world.conn, "PACKET-01", "building")
        world.conn.execute(
            "UPDATE packets SET lease_owner=?, lease_expires=? WHERE id=?",
            ("dead-host:999:abcd", time.time() - 10, "PACKET-01"))

    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["lease_owner"] is None
    assert row["status"] == "awaiting_ci"
    assert world.events("PACKET-01", "lease_expired")


def test_a_live_lease_is_not_stolen(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    with store.write(world.conn):
        store.upsert_spec(world.conn, "PACKET-01", "loop/packet-01")
        store.acquire(world.conn, "PACKET-01", "other-controller", 3600)

    called = []
    monkeypatch.setattr(agents, "run_builder",
                        lambda *a, **k: called.append(1) or {"outcome": "exited", "code": 0,
                                                             "wall": 1, "detail": "",
                                                             "tokens": None})
    world.controller().tick()
    assert called == []


# --- 7. escalation -----------------------------------------------------------


def test_builder_escalation_is_read_from_the_worktree(world, monkeypatch):
    """The previous design read the packet file in the primary checkout
    while the builder wrote one inside its worktree, so escalation never
    reached the controller at all."""
    def escalate(worktree):
        (worktree / ".loop").mkdir(exist_ok=True)
        (worktree / ".loop" / "result.json").write_text(json.dumps({
            "outcome": "escalated",
            "summary": "the pinned test contradicts PRD-02",
            "escalation": {"reason": "test asserts MD approves above 4M",
                           "spec_clause": "PRD-02 §2.4",
                           "test": "tests/acceptance/test_thing.py::test_thing",
                           "should_assert": "chairman above 4M"},
        }))

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(escalate))

    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "escalated"
    assert "PRD-02 §2.4" in row["reason"]
    assert world.pushed == []


def test_escalation_without_a_spec_citation_is_rejected(world, monkeypatch):
    def vague(worktree):
        (worktree / ".loop").mkdir(exist_ok=True)
        (worktree / ".loop" / "result.json").write_text(json.dumps(
            {"outcome": "escalated", "escalation": {"reason": "feels wrong"}}))

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(vague))

    world.controller().tick()
    assert world.attempts("PACKET-01")[0]["outcome"] == "bad_result"


def test_a_stale_result_file_cannot_be_read_as_this_attempts_answer(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()

    calls = []

    def first_escalate_then_build(worktree):
        calls.append(1)
        if len(calls) == 1:
            (worktree / ".loop").mkdir(exist_ok=True)
            (worktree / ".loop" / "result.json").write_text(json.dumps(
                {"outcome": "gave_up", "summary": "stuck"}))
        else:
            commits(worktree)

    monkeypatch.setattr(agents, "run_builder", fake_builder(first_escalate_then_build))
    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "queued"
    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "awaiting_ci"


# --- the oracle --------------------------------------------------------------


def test_touching_a_pinned_acceptance_file_escalates_and_opens_no_pr(world, monkeypatch):
    def tamper(worktree):
        (worktree / "tests" / "acceptance" / "test_thing.py").write_text(
            "def test_thing():\n    pass\n")
        commits(worktree, message="weaken the test")

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(tamper))

    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "escalated"
    assert "pinned acceptance files changed" in row["reason"]
    assert world.pushed == []


def test_deleting_a_pinned_acceptance_file_is_caught(world, monkeypatch):
    def delete(worktree):
        (worktree / "tests" / "acceptance" / "test_thing.py").unlink()
        commits(worktree, message="remove the test")

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(delete))
    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "escalated"


def test_new_unit_tests_are_allowed(world, monkeypatch):
    """ED-7 requires the builder to write unit and integration tests. Only
    the pinned files are frozen — the previous design's prompt forbade all
    of tests/, which made the definition of done unsatisfiable."""
    def add_unit_test(worktree):
        path = worktree / "tests" / "unit" / "test_new.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_new():\n    assert True\n")
        commits(worktree, message="add unit tests")

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(add_unit_test))

    world.controller().tick()
    assert world.row("PACKET-01")["status"] == "awaiting_ci"


def test_builder_cannot_commit_controller_or_packet_governance(world, monkeypatch):
    def edit_packet(worktree):
        packet = worktree / "docs" / "packets" / "PACKET-01.md"
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_text("builder changed its own definition\n")
        commits(
            worktree,
            message="change packet",
            touch="docs/packets/PACKET-01.md",
        )

    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(edit_packet))

    world.controller().tick()

    assert world.row("PACKET-01")["status"] == "escalated"
    assert world.attempts("PACKET-01")[0]["outcome"] == "governance_violation"
    assert world.pushed == []


def test_a_packet_with_no_acceptance_tests_is_never_dispatched(world, monkeypatch):
    world.add_packet("PACKET-01", tests=())
    world.pin_oracle()
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *a, **k: called.append(1))

    world.controller().tick()

    assert called == []
    assert world.row("PACKET-01")["status"] == "escalated"
    assert "no acceptance tests" in world.row("PACKET-01")["reason"]


# --- 8 & 9. merge and reconciliation -----------------------------------------


def test_merge_ready_waits_when_auto_merge_is_off(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, t: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps({"verdict": "approve", "blocking": []})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])
    world.controller().tick()
    world.controller().tick()

    assert world.row("PACKET-01")["status"] == "merge_ready"
    assert world.merged == []


def test_auto_merge_lands_a_routine_packet_when_both_identities_exist(world, monkeypatch):
    world.config["github"]["auto_merge"] = True
    world.config["github"]["builder_token_env"] = "FAKE_BUILDER"
    world.config["github"]["reviewer_token_env"] = "FAKE_REVIEWER"
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, t: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps({"verdict": "approve", "blocking": []})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])
    world.controller().tick()
    world.controller().tick()

    assert world.merged == [100]
    assert world.row("PACKET-01")["status"] == "merged"


def test_blast_radius_escalates_even_on_approval(world, monkeypatch):
    world.config["github"]["auto_merge"] = True
    world.add_packet("PACKET-01", blast=True)
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()
    world.ci[100] = {"verdict": "green", "failing": [], "unreported": [], "runs": {}}
    monkeypatch.setattr(agents, "run_reviewer", lambda c, r, rd, p, t: (
        rd.mkdir(parents=True, exist_ok=True),
        (rd / "verdict.json").write_text(json.dumps({"verdict": "approve", "blocking": []})),
        {"outcome": "exited", "code": 0, "wall": 1, "detail": ""})[-1])
    world.controller().tick()

    row = world.row("PACKET-01")
    assert row["status"] == "escalated"
    assert "blast_radius" in row["reason"]
    assert world.merged == []


def test_a_routine_packet_that_touches_a_blast_path_is_corrected(world, monkeypatch):
    def touch_migration(worktree):
        path = worktree / "platform" / "claim_core" / "alembic" / "versions" / "0015.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# migration\n")
        commits(
            worktree,
            message="add migration",
            touch="platform/claim_core/alembic/versions/x.py",
        )

    world.add_packet("PACKET-01", blast=False)
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(touch_migration))
    world.controller().tick()

    assert world.events("PACKET-01", "blast_radius_corrected")


def test_a_pr_merged_by_hand_is_reconciled(world, monkeypatch):
    """Otherwise the packet sits at merge_ready and everything downstream
    stays blocked on a dependency that is, in fact, merged."""
    world.add_packet("PACKET-01")
    world.add_packet("PACKET-02", depends_on=["PACKET-01"])
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    world.pr_states[100] = "MERGED"
    ctl = world.controller()
    ctl.reconcile()

    assert world.row("PACKET-01")["status"] == "merged"
    assert [p.id for p, _ in ctl.selectable()] == ["PACKET-02"]


def test_github_merge_overrides_a_stale_local_escalation(world):
    """A blast-radius/manual escalation is not terminal after its PR merges.

    The owner may merge the PR while the controller still carries the reason
    that stopped automation.  Reconciliation must trust that exact PR's merged
    state so dependants are not left blocked on stale local bookkeeping.
    """
    world.add_packet("PACKET-01", status="escalated")
    world.add_packet("PACKET-02", depends_on=["PACKET-01"])
    world.pin_oracle()
    ctl = world.controller(synced=True)
    with store.write(world.conn):
        store.set_fields(
            world.conn,
            "PACKET-01",
            pr_number=100,
            reason="approved and green; owner merge required for blast radius",
        )
    world.pr_states[100] = "MERGED"

    ctl.reconcile()

    assert world.row("PACKET-01")["status"] == "merged"
    assert world.row("PACKET-01")["reason"] is None
    assert [packet.id for packet, _ in ctl.selectable()] == ["PACKET-02"]


def test_a_pr_closed_without_merging_escalates(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    world.pr_states[100] = "CLOSED"
    world.controller().reconcile()
    assert world.row("PACKET-01")["status"] == "escalated"


# --- dependencies, bootstrap, breakers ---------------------------------------


def test_dependencies_gate_dispatch(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.add_packet("PACKET-02", depends_on=["PACKET-01"])
    world.pin_oracle()
    ctl = world.controller(synced=True)
    assert [p.id for p, _ in ctl.selectable()] == ["PACKET-01"]


def test_bootstrap_does_not_resurrect_merged_packets(world):
    """First contact with a board of 22 already-merged packets must not
    queue all of them and rebuild the product."""
    world.add_packet("PACKET-01", status="merged")
    world.add_packet("PACKET-02", status="queued", depends_on=["PACKET-01"])
    world.pin_oracle()
    ctl = world.controller(synced=False)
    with store.write(world.conn):
        for packet in P.load_board(ctl.board_dir):
            store.upsert_spec(world.conn, packet.id, packet.meta["branch"],
                              bootstrap_status=packet.meta["status"])
    assert world.row("PACKET-01")["status"] == "merged"
    assert [p.id for p, _ in ctl.selectable()] == ["PACKET-02"]


def test_the_pause_file_stops_everything(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    (world.repo / "loop" / "PAUSED").write_text("owner is re-slicing PRD-10")
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *a, **k: called.append(1))

    world.controller().tick()
    assert called == []


def test_a_failed_preflight_pauses_the_loop_rather_than_failing_packets(world, monkeypatch):
    """The previous design had no preflight: a machine that could not run
    ruff produced infra_error on every packet, which by design did not trip
    the bad-slice breaker, so it failed quietly for as long as you let it."""
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(gates, "preflight", lambda *a, **k: ["ruff is not on PATH"])
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *a, **k: called.append(1))

    ctl = world.controller()
    ctl.tick()

    assert called == []
    assert world.row("PACKET-01")["status"] == "queued"
    assert any("ruff" in line for line in ctl.log)


def test_concurrency_cap_is_respected(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.add_packet("PACKET-02")
    world.pin_oracle()
    monkeypatch.setattr(agents, "run_builder", fake_builder(lambda wt: commits(wt)))
    world.controller().tick()

    assert sum(1 for r in store.all_packets(world.conn)
               if r["status"] == "awaiting_ci") == 1


# --- the dry run must be dry -------------------------------------------------


def test_dry_run_writes_nothing(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *a, **k: called.append(1))

    before = {p: p.read_text() for p in (world.repo / "docs" / "packets").glob("*.md")}
    ctl = world.controller(dry_run=True)
    ctl.tick()

    assert called == []
    assert world.row("PACKET-01") is None          # nothing registered
    assert not (world.repo / ".claude" / "worktrees").exists()
    assert {p: p.read_text() for p in (world.repo / "docs" / "packets").glob("*.md")} == before
    assert any("WOULD" in line for line in ctl.log)


def test_dry_run_does_not_fetch_or_update_git_refs(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    world.controller(synced=True)
    monkeypatch.setattr(
        forge,
        "resolve_base",
        lambda *args, **kwargs: pytest.fail("dry run fetched origin/main"),
    )
    monkeypatch.setattr(
        forge,
        "git",
        lambda args, cwd, **kwargs: world.base_sha
        if args == ["rev-parse", "origin/main"]
        else "",
    )

    world.controller(dry_run=True).tick()

    assert world.row("PACKET-01")["base_sha"] is None


# --- identity coherence ------------------------------------------------------


def test_auto_merge_with_one_identity_is_a_preflight_failure(monkeypatch):
    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    config["github"]["auto_merge"] = True
    config["github"]["builder_token_env"] = "SAME_TOKEN"
    config["github"]["reviewer_token_env"] = "SAME_TOKEN"
    monkeypatch.setenv("SAME_TOKEN", "ghp_whatever")
    problems = gates.identity_problems(config)
    assert any("same token" in p for p in problems)


def test_auto_merge_without_tokens_is_a_preflight_failure():
    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    config["github"]["auto_merge"] = True
    problems = gates.identity_problems(config)
    assert any("two distinct identities" in p for p in problems)


def test_single_identity_without_auto_merge_is_fine():
    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    assert gates.identity_problems(config) == []


def test_configured_but_missing_builder_credential_pauses_even_without_auto_merge():
    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    config["github"]["builder_token_env"] = "MISSING_BUILDER_TOKEN"
    problems = gates.identity_problems(config)
    assert any("MISSING_BUILDER_TOKEN" in problem for problem in problems)


# --- adversarial regressions -------------------------------------------------


def test_gave_up_with_commits_is_not_published(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(lambda worktree: commits(worktree, result="gave_up")),
    )

    world.controller().tick()

    assert world.row("PACKET-01")["status"] != "awaiting_ci"
    assert world.pushed == []


def test_missing_result_with_commits_is_not_published(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()

    def commit_without_result(worktree):
        commits(worktree)
        (worktree / ".loop" / "result.json").unlink()

    monkeypatch.setattr(agents, "run_builder", fake_builder(commit_without_result))
    world.controller().tick()

    assert world.row("PACKET-01")["status"] != "awaiting_ci"
    assert world.pushed == []


def test_new_unpinned_acceptance_file_cannot_be_authored_by_builder(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-00", status="merged")
    world.pin_oracle()
    world.add_packet(
        "PACKET-01",
        depends_on=("PACKET-00",),
        tests=("tests/acceptance/test_new_oracle.py",),
    )
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *args, **kwargs: called.append(1))

    world.controller().tick()

    assert called == []
    assert world.row("PACKET-01") is not None
    assert world.row("PACKET-01")["status"] == "queued"


def test_effective_blast_radius_survives_until_approval(world, monkeypatch):
    world.config["github"]["auto_merge"] = True
    world.add_packet("PACKET-01", blast=False)
    world.pin_oracle()
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(
            lambda worktree: commits(
                worktree,
                message="migration",
                touch="platform/claim_core/alembic/versions/0015.py",
            )
        ),
    )
    world.controller().tick()
    world.ci[100] = {
        "verdict": "green",
        "failing": [],
        "unreported": [],
        "runs": {},
    }
    monkeypatch.setattr(
        agents,
        "run_reviewer",
        lambda *args, **kwargs: {
            "outcome": "exited",
            "code": 0,
            "wall": 1,
            "detail": "",
            "structured": {
                "verdict": "approve",
                "blocking": [],
                "non_blocking": [],
                "judgement_calls": [],
                "escalation": None,
            },
        },
    )

    world.controller().tick()

    assert world.row("PACKET-01")["status"] == "escalated"
    assert world.row("PACKET-01")["effective_blast_radius"] == 1
    assert world.merged == []


def test_tick_does_not_dirty_primary_checkout_with_runtime_state(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    git(["add", "-A"], world.repo)
    git(["commit", "-qm", "board"], world.repo)
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(lambda worktree: commits(worktree)),
    )

    world.controller().tick()

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=world.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "loop/digest.md" not in status
    assert "docs/packets/PACKET-01.md" not in status


def test_stale_overlapping_review_cannot_record_or_apply_second_verdict(
    world,
    monkeypatch,
):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    ctl = world.controller(synced=True)
    with store.write(world.conn):
        store.set_fields(world.conn, "PACKET-01", pr_number=100)
        store.set_status(world.conn, "PACKET-01", "review")
    stale_row = world.row("PACKET-01")
    verdicts = iter(
        [
            {
                "verdict": "approve",
                "blocking": [],
                "non_blocking": [],
                "judgement_calls": [],
                "escalation": None,
            },
            {
                "verdict": "rework",
                "blocking": ["second reviewer overwrote approval"],
                "non_blocking": [],
                "judgement_calls": [],
                "escalation": None,
            },
        ]
    )
    monkeypatch.setattr(
        agents,
        "run_reviewer",
        lambda *args, **kwargs: {
            "outcome": "exited",
            "code": 0,
            "wall": 1,
            "detail": "",
            "structured": next(verdicts),
        },
    )

    ctl.advance_review(stale_row)
    ctl.advance_review(stale_row)

    assert world.row("PACKET-01")["status"] == "merge_ready"
    assert len(world.events("PACKET-01", "review_verdict")) == 1


def test_controller_lease_blocks_an_overlapping_tick(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    called = []
    monkeypatch.setattr(agents, "run_builder", lambda *args, **kwargs: called.append(1))
    with store.write(world.conn):
        assert store.acquire_controller(world.conn, "other-controller", 3600)

    ctl = world.controller()
    ctl.tick()

    assert called == []
    assert world.row("PACKET-01") is None
    assert any("another controller" in line for line in ctl.log)


def test_changed_pr_head_is_never_reviewed(world, monkeypatch):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    monkeypatch.setattr(
        agents,
        "run_builder",
        fake_builder(lambda worktree: commits(worktree)),
    )
    world.controller().tick()
    world.ci[100] = {
        "verdict": "green",
        "failing": [],
        "unreported": [],
        "runs": {},
        "head_sha": "attacker-replaced-head",
    }
    called = []
    monkeypatch.setattr(agents, "run_reviewer", lambda *args, **kwargs: called.append(1))

    world.controller().tick()

    assert called == []
    assert world.row("PACKET-01")["status"] == "escalated"


def test_audit_snapshot_restores_an_empty_ledger(world, tmp_path):
    world.add_packet("PACKET-01")
    world.pin_oracle()
    world.controller(synced=True)
    with store.write(world.conn):
        store.set_status(world.conn, "PACKET-01", "blocked", reason="test")
    snapshot = store.export_state(world.conn)

    restored = store.open_db(tmp_path / "restored.db")
    try:
        store.init(restored)
        store.restore_state(restored, snapshot)
        assert store.get(restored, "PACKET-01")["status"] == "blocked"
        assert len(store.events(restored, "PACKET-01")) == len(
            store.events(world.conn, "PACKET-01")
        )
    finally:
        restored.close()


def test_audit_publication_pushes_recoverable_state_to_dedicated_branch(
    tmp_path,
    monkeypatch,
):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(["init", "--bare", "-q", str(remote)], tmp_path)
    git(["init", "-q", "-b", "main", str(repo)], tmp_path)
    git(["config", "user.email", "loop@test"], repo)
    git(["config", "user.name", "loop"], repo)
    (repo / "README.md").write_text("base\n")
    (repo / ".gitignore").write_text(".claude/worktrees/\n")
    git(["add", "README.md", ".gitignore"], repo)
    git(["commit", "-qm", "base"], repo)
    git(["remote", "add", "origin", str(remote)], repo)
    git(["push", "-q", "-u", "origin", "main"], repo)

    conn = store.open_db(tmp_path / "audit-state.db")
    try:
        store.init(conn)
        with store.write(conn):
            store.upsert_spec(conn, "PACKET-01", "loop/packet-01")
            store.set_status(conn, "PACKET-01", "blocked", reason="test")
        config = {
            "audit": {"enabled": True, "branch": "automation/loop-state"},
            "github": {"repository": "owner/repo"},
        }
        monkeypatch.setattr(audit, "REPO", repo)
        monkeypatch.setattr(forge, "verify_repo_identity", lambda *args, **kwargs: None)

        head = audit.publish(conn, config, "# digest\n")

        assert head
        snapshot = json.loads(
            subprocess.run(
                [
                    "git",
                    "show",
                    "origin/automation/loop-state:audit/loop-state.json",
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        assert snapshot["packets"][0]["status"] == "blocked"
        assert audit.publish(conn, config, "# changed timestamp only\n") is None
        primary_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert primary_status == ""
    finally:
        conn.close()


def test_agent_environment_excludes_controller_credentials(monkeypatch):
    config = yaml.safe_load((pathlib.Path(__file__).parent / "config.yml").read_text())
    config["github"]["builder_token_env"] = "PACHA_BUILDER_TOKEN"
    config["github"]["reviewer_token_env"] = "PACHA_REVIEWER_TOKEN"
    monkeypatch.setenv("PACHA_BUILDER_TOKEN", "builder-secret")
    monkeypatch.setenv("PACHA_REVIEWER_TOKEN", "reviewer-secret")
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")

    env = agents._agent_env(config)

    assert "PACHA_BUILDER_TOKEN" not in env
    assert "PACHA_REVIEWER_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "DATABASE_URL" not in env
