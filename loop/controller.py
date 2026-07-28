#!/usr/bin/env python3
"""The controller. One state machine, one writer, one tick.

Every status transition in the loop happens in this file. Agents return
evidence; the controller decides what it means. Nothing an agent writes to a
packet file has any effect.

A tick, in order:

    1. reconcile   — expired leases, and GitHub's view of every open PR
    2. preflight   — can this machine build at all?
    3. breakers    — global caps and the bad-slice detector
    4. advance     — awaiting_ci -> review | rework, review -> verdict,
                     merge_ready -> merged
    5. dispatch    — queued and rework packets, up to the concurrency cap
    6. audit       — publish the ledger snapshot on a controller-only branch

Order matters. Reconcile first, so a PR merged by hand between ticks stops
blocking its dependants. Breakers before dispatch, so a tripped loop starts
nothing — including packets that look fine on their own, which are exactly
the expensive ones when the slice is bad.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import agents  # noqa: E402
import audit  # noqa: E402
import forge  # noqa: E402
import gates  # noqa: E402
import packets as P  # noqa: E402
import store  # noqa: E402

REPO = P.REPO
PAUSE_FILE = REPO / "loop" / "PAUSED"


def _pr_number(value) -> int | None:
    """Pull a PR number out of whatever the committed bootstrap history holds — a
    URL from the pre-controller board, an int, or nothing."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and "/pull/" in value:
        return int(value.rstrip("/").rsplit("/", 1)[-1])
    return None


class Controller:
    def __init__(self, config: dict, conn, *, dry_run: bool = False, board_dir=None):
        self.config = config
        self.conn = conn
        self.dry = dry_run
        self.board_dir = REPO / (board_dir or config["board_dir"])
        self.owner = store.owner_token()
        self.log: list[str] = []
        self._tick_lease_active = False

    # --- plumbing ------------------------------------------------------------

    def say(self, message: str) -> None:
        self.log.append(message)
        print(message)

    def would(self, message: str) -> None:
        self.say(f"  WOULD: {message}")

    def packet_spec(self, packet_id: str) -> P.Packet:
        for packet in P.load_board(self.board_dir):
            if packet.id == packet_id:
                return packet
        raise P.SpecError(f"{packet_id} is in the ledger but not on the board")

    def worktree_for(self, packet_id: str) -> pathlib.Path:
        return REPO / ".claude" / "worktrees" / f"loop-{packet_id.lower()}"

    def run_dir(self, packet_id: str, attempt: int) -> pathlib.Path:
        return REPO / "loop" / "runs" / packet_id / f"attempt-{attempt}"

    def review_worktree_for(self, packet_id: str) -> pathlib.Path:
        return REPO / ".claude" / "worktrees" / f"review-{packet_id.lower()}"

    def transition(
        self,
        packet_id: str,
        status: str,
        *,
        reason=None,
        expected=None,
        **payload,
    ) -> bool:
        if self.dry:
            self.would(f"{packet_id} -> {status}" + (f" ({reason})" if reason else ""))
            return True
        with store.write(self.conn):
            changed = store.set_status(
                self.conn,
                packet_id,
                status,
                reason=reason,
                expected_status=expected["status"] if expected is not None else None,
                expected_version=expected["version"] if expected is not None else None,
                **payload,
            )
        if not changed:
            self.say(f"{packet_id}: stale {status} transition ignored")
        return changed

    def sync_board(self) -> None:
        """Register any packet the board declares that the ledger has not
        seen. Creating a row is the only thing this does — a packet already
        in the ledger is never moved by re-reading its spec."""
        with store.write(self.conn):
            for packet in P.load_board(self.board_dir):
                store.upsert_spec(
                    self.conn, packet.id, packet.meta["branch"],
                    bootstrap_status=packet.meta.get("status", "queued"),
                    pr_number=_pr_number(packet.meta.get("pr")),
                    attempts=int(packet.meta.get("attempts") or 0),
                    reason=packet.meta.get("reason"),
                    declared_blast_radius=packet.meta["blast_radius"],
                )

    @contextlib.contextmanager
    def controller_lease(self):
        """Own every external action in a tick, with a renewable DB lease."""
        if self.dry:
            yield True
            return
        ttl = self.config["breakers"]["controller_lease_seconds"]
        with store.write(self.conn):
            acquired = store.acquire_controller(self.conn, self.owner, ttl)
        if not acquired:
            self.say("another controller owns this tick; skipping")
            yield False
            return

        database = pathlib.Path(
            self.conn.execute("PRAGMA database_list").fetchone()["file"]
        )
        stopped = threading.Event()

        def heartbeat() -> None:
            while not stopped.wait(max(1, ttl // 3)):
                try:
                    with store.connect(database) as heartbeat_conn:
                        store.init(heartbeat_conn)
                        with store.write(heartbeat_conn):
                            if not store.renew_controller(
                                heartbeat_conn, self.owner, ttl
                            ):
                                return
                except Exception as exc:  # visible, and lease expiry stops progress
                    self.say(f"controller lease heartbeat failed: {exc}")
                    return

        thread = threading.Thread(
            target=heartbeat,
            name="pacha-loop-controller-lease",
            daemon=True,
        )
        thread.start()
        self._tick_lease_active = True
        try:
            yield True
        finally:
            self._tick_lease_active = False
            stopped.set()
            thread.join(timeout=5)
            with store.write(self.conn):
                store.release_controller(self.conn, self.owner)

    def renew_controller_lease(self) -> bool:
        """Synchronously prove ownership immediately before a side effect."""
        if self.dry or not self._tick_lease_active:
            return True
        ttl = self.config["breakers"]["controller_lease_seconds"]
        with store.write(self.conn):
            owned = store.renew_controller(self.conn, self.owner, ttl)
        if not owned:
            self.say("controller lease lost; refusing further external actions")
        return owned

    # --- 1. reconcile --------------------------------------------------------

    def reconcile(self) -> None:
        """Make the ledger agree with reality before acting on it.

        Two sources of drift the previous design had no answer for: a
        controller that died mid-build (lease never released, packet stuck
        in `building` forever) and a PR the owner merged by hand (packet
        stuck at `merge_ready`, everything downstream blocked).
        """
        for row in store.expired_leases(self.conn):
            self.say(f"reclaiming expired lease: {row['id']} (was {row['status']})")
            if self.dry:
                continue
            with store.write(self.conn):
                self.conn.execute(
                    "UPDATE packets SET lease_owner=NULL, lease_expires=NULL WHERE id=?",
                    (row["id"],),
                )
                store.record(self.conn, row["id"], "lease_expired", frm=row["status"],
                             previous_owner=row["lease_owner"])
                if row["status"] in store.LEASED:
                    attempt = self.conn.execute(
                        "SELECT id FROM attempts WHERE packet_id=? AND ended_at IS NULL"
                        " ORDER BY id DESC LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    if attempt:
                        store.finish_attempt(
                            self.conn,
                            attempt["id"],
                            "controller_crash",
                            detail="controller lease expired before attempt completion",
                        )
                    store.set_status(
                        self.conn,
                        row["id"],
                        "queued",
                        kind="crash_recovery",
                        reason="controller died mid-attempt; lease expired",
                    )

        for row in store.all_packets(self.conn):
            if row["status"] not in store.OPEN_ON_GITHUB or not row["pr_number"]:
                continue
            try:
                state = forge.pr_state(REPO, row["pr_number"],
                                       token_env=self._token("reviewer"))
            except forge.ForgeError as exc:
                self.say(f"  cannot read PR #{row['pr_number']} for {row['id']}: {exc}")
                continue
            if row["head_sha"]:
                try:
                    forge.verify_pr_identity(
                        state,
                        expected_head_sha=row["head_sha"],
                        expected_head_ref=row["branch"],
                    )
                except forge.ForgeError as exc:
                    self.transition(
                        row["id"],
                        "escalated",
                        expected=row,
                        kind="pr_identity_changed",
                        reason=str(exc),
                    )
                    continue
            if state["state"] == "MERGED":
                self.say(f"{row['id']}: PR #{row['pr_number']} is merged on GitHub")
                self.transition(
                    row["id"],
                    "merged",
                    expected=row,
                    kind="reconciled",
                    merge_commit=state["merge_commit"],
                )
            elif state["state"] == "CLOSED":
                self.say(f"{row['id']}: PR #{row['pr_number']} was closed without merging")
                self.transition(
                    row["id"],
                    "escalated",
                    expected=row,
                    kind="reconciled",
                    reason=f"PR #{row['pr_number']} closed without merging",
                )

    # --- 2 & 3. gates on the whole loop --------------------------------------

    def _token(self, role: str) -> str | None:
        github = self.config.get("github") or {}
        return github.get(f"{role}_token_env") or None

    def blockers(self) -> list[str]:
        problems = []
        if PAUSE_FILE.exists():
            problems.append(f"loop/PAUSED exists: {PAUSE_FILE.read_text().strip()}")
        problems += gates.preflight(self.config)
        try:
            board = P.load_board(self.board_dir)
            locked = P.load_oracle()
            differences = P.oracle_differences(board, REPO, locked)
            if differences:
                problems.append(
                    "acceptance oracle does not exactly match the board: "
                    + "; ".join(differences[:10])
                )
        except P.SpecError as exc:
            problems.append(str(exc))
        problems += self.breakers()
        return problems

    def breakers(self) -> list[str]:
        cfg = self.config["breakers"]
        spend = store.attempts_today(self.conn)
        out = []
        if spend["builder_minutes"] >= cfg["daily_builder_minutes"]:
            out.append(f"daily_builder_minutes: {spend['builder_minutes']} of "
                       f"{cfg['daily_builder_minutes']} used today")
        if spend["attempts"] >= cfg["daily_attempts"]:
            out.append(f"daily_attempts: {spend['attempts']} of {cfg['daily_attempts']} today")

        window = store.recent_outcomes(self.conn, cfg["blocked_ratio_window"])
        if len(window) >= cfg["blocked_ratio_min_sample"]:
            spec = [r for r in window if r["status"] == "blocked"
                    and r["outcome"] in ("gate_failed", "timeout", "oracle_violation",
                                         "dirty_worktree", "no_commits")]
            infra = [r for r in window if r["status"] == "blocked"
                     and r["outcome"] in ("infra_error", "spawn_failed")]
            if len(spec) / len(window) > cfg["blocked_ratio_threshold"]:
                out.append(
                    f"blocked_ratio: {len(spec)}/{len(window)} recent packets blocked on their "
                    f"own acceptance tests. The slice is wrong, not the builders. Re-slice "
                    f"before restarting: {[r['id'] for r in spec]}"
                )
            if len(infra) / len(window) > cfg["infra_blocked_ratio_threshold"]:
                out.append(
                    f"infra_blocked_ratio: {len(infra)}/{len(window)} packets died before their "
                    f"tests ran. This is the environment, not the slice."
                )
        return out

    # --- 4. advance ----------------------------------------------------------

    def advance(self, max_passes: int = 4) -> None:
        """Run the non-dispatch transitions until they stop producing change.

        One pass is not enough: CI going green moves a packet to `review`,
        and the reviewer's verdict can move it to `merge_ready`, and an
        auto-merge can move it to `merged`. A single pass would take three
        ticks to do what one tick can, which on an hourly schedule is three
        hours of nothing happening. Bounded so a transition that oscillates
        cannot spin.
        """
        for _ in range(max_passes):
            before = {r["id"]: r["status"] for r in store.all_packets(self.conn)}
            for row in store.all_packets(self.conn):
                if row["status"] == "awaiting_ci":
                    self.advance_ci(row)
                elif row["status"] == "review":
                    self.advance_review(row)
                elif row["status"] == "merge_ready":
                    self.advance_merge(row)
            after = {r["id"]: r["status"] for r in store.all_packets(self.conn)}
            if after == before:
                return

    def advance_ci(self, row) -> None:
        packet_id, pr = row["id"], row["pr_number"]
        state = forge.ci_state(REPO, pr, self.config["github"]["required_checks"],
                               token_env=self._token("builder"))
        try:
            forge.verify_pr_identity(
                state,
                expected_head_sha=row["head_sha"],
                expected_head_ref=row["branch"],
            )
        except forge.ForgeError as exc:
            self.transition(
                packet_id,
                "escalated",
                expected=row,
                kind="pr_identity_changed",
                reason=str(exc),
            )
            return
        if state["verdict"] == "pending":
            self.say(f"{packet_id}: CI pending ({', '.join(state['unreported'])})")
            return
        if state["verdict"] == "green":
            self.say(f"{packet_id}: CI green -> review")
            self.transition(packet_id, "review", expected=row, kind="ci_green")
            return

        # Red CI is a rework, not a mystery. Feed the failing output back.
        logs = forge.failing_logs(REPO, pr, token_env=self._token("builder"))
        feedback = (
            "YOUR PREVIOUS ATTEMPT PASSED THE LOCAL GATE BUT CI IS RED.\n\n"
            f"Failing required checks: {', '.join(state['failing'])}\n\n"
            f"{logs}\n\n"
            "Fix the cause. Do not weaken a test to make the check pass."
        )
        self.say(f"{packet_id}: CI red ({', '.join(state['failing'])}) -> rework")
        self.send_back(packet_id, row, feedback, source="ci_red")

    def advance_review(self, row) -> None:
        packet_id = row["id"]
        spec = self.packet_spec(packet_id)
        attempt = row["attempts"]
        run_dir = self.run_dir(packet_id, attempt) / "review"
        review_worktree = self.review_worktree_for(packet_id)
        verdict_path = run_dir / "verdict.json"
        prompt = agents.reviewer_prompt(
            packet_id, str(spec.path.relative_to(REPO)), row["pr_number"])

        if self.dry:
            self.would(f"run reviewer for {packet_id} on PR #{row['pr_number']}")
            return

        # Legacy file output is accepted only by the test seam. Production
        # Claude returns JSON-schema output on stdout and has no Write tool.
        if verdict_path.exists():
            verdict_path.unlink()
        try:
            before = forge.pr_state(
                REPO, row["pr_number"], token_env=self._token("reviewer")
            )
            forge.verify_pr_identity(
                before,
                expected_head_sha=row["head_sha"],
                expected_head_ref=row["branch"],
            )
        except forge.ForgeError as exc:
            self.transition(
                packet_id,
                "escalated",
                expected=row,
                kind="pr_identity_changed",
                reason=str(exc),
            )
            return

        if not self.renew_controller_lease():
            return
        try:
            forge.ensure_review_worktree(REPO, review_worktree, row["head_sha"])
            forge.verify_repo_identity(
                review_worktree,
                self.config["github"]["repository"],
            )
            result = agents.run_reviewer(
                self.config,
                review_worktree,
                run_dir,
                prompt,
                self.config["breakers"]["review_wall_seconds"],
            )
        except forge.ForgeError as exc:
            self.transition(
                packet_id,
                "escalated",
                expected=row,
                reason=f"could not isolate reviewer checkout: {exc}",
            )
            return
        finally:
            forge.remove_worktree(REPO, review_worktree)
        if not self.renew_controller_lease():
            return
        if result["outcome"] != "exited" or result.get("code") != 0:
            self.say(f"{packet_id}: reviewer produced no verdict ({result['outcome']})")
            self.transition(packet_id, "escalated",
                            expected=row,
                            reason=f"reviewer produced no verdict: {result['outcome']}. "
                                   f"See {run_dir}")
            return
        try:
            if result.get("structured") is not None:
                verdict = agents.validate_review_verdict(result["structured"])
            elif verdict_path.exists():  # compatibility for deterministic tests
                verdict = agents.validate_review_verdict(
                    json.loads(verdict_path.read_text())
                )
            else:
                raise agents.AgentError(
                    result.get("structured_error") or "reviewer returned no structured verdict"
                )
            after = forge.pr_state(
                REPO, row["pr_number"], token_env=self._token("reviewer")
            )
            forge.verify_pr_identity(
                after,
                expected_head_sha=row["head_sha"],
                expected_head_ref=row["branch"],
            )
        except (agents.AgentError, json.JSONDecodeError, forge.ForgeError) as exc:
            self.transition(packet_id, "escalated",
                            expected=row,
                            reason=f"reviewer verdict is unusable: {exc}")
            return

        if verdict["verdict"] == "escalate":
            self.apply_review_status(
                row,
                verdict,
                "escalated",
                kind="reviewer_escalated",
                reason=verdict["escalation"],
            )
        elif verdict["verdict"] == "rework":
            feedback = ("THE REVIEWER RETURNED BLOCKING FINDINGS. Fix each one.\n\n"
                        + "\n".join(f"  {i}. {f}" for i, f in enumerate(verdict["blocking"], 1)))
            self.send_back(
                packet_id,
                row,
                feedback,
                source="reviewer",
                review_verdict=verdict,
            )
        elif row["effective_blast_radius"]:
            self.apply_review_status(
                row,
                verdict,
                "escalated",
                kind="blast_radius",
                reason="approved and green, escalated because blast_radius is true. "
                "Nothing is wrong with it; it needs your merge.",
                judgement_calls=verdict["judgement_calls"],
            )
        else:
            self.apply_review_status(
                row,
                verdict,
                "merge_ready",
                kind="approved",
                judgement_calls=verdict["judgement_calls"],
            )

    def apply_review_status(
        self,
        row,
        verdict: dict,
        status: str,
        *,
        reason=None,
        kind: str,
        **payload,
    ) -> bool:
        """Atomically persist one verdict and its resulting transition."""
        if self.dry:
            self.would(f"{row['id']} -> {status}")
            return True
        with store.write(self.conn):
            fresh = store.get(self.conn, row["id"])
            if (
                fresh["status"] != row["status"]
                or fresh["version"] != row["version"]
            ):
                self.say(f"{row['id']}: stale review verdict ignored")
                return False
            store.record(self.conn, row["id"], "review_verdict", **verdict)
            return store.set_status(
                self.conn,
                row["id"],
                status,
                reason=reason,
                kind=kind,
                expected_status=row["status"],
                expected_version=row["version"],
                **payload,
            )

    def advance_merge(self, row) -> None:
        github = self.config.get("github") or {}
        if not github.get("auto_merge"):
            self.say(f"{row['id']}: approved and green, waiting on your merge "
                     f"(PR #{row['pr_number']})")
            return
        packet_id, pr = row["id"], row["pr_number"]
        if self.dry:
            self.would(f"approve and merge PR #{pr} for {packet_id}")
            return
        try:
            state = forge.ci_state(
                REPO,
                pr,
                self.config["github"]["required_checks"],
                token_env=self._token("reviewer"),
            )
            forge.verify_pr_identity(
                state,
                expected_head_sha=row["head_sha"],
                expected_head_ref=row["branch"],
            )
            if state["verdict"] != "green":
                raise forge.ForgeError(
                    f"required checks are {state['verdict']} immediately before merge"
                )
            if not self.renew_controller_lease():
                return
            forge.approve(REPO, pr, "Approved by the loop reviewer. See the packet file.",
                          token_env=self._token("reviewer"))
            state = forge.pr_state(REPO, pr, token_env=self._token("reviewer"))
            forge.verify_pr_identity(
                state,
                expected_head_sha=row["head_sha"],
                expected_head_ref=row["branch"],
            )
            if not self.renew_controller_lease():
                return
            commit = forge.merge(REPO, pr, token_env=self._token("reviewer"))
        except forge.ForgeError as exc:
            self.say(f"{packet_id}: merge refused — {exc}")
            self.transition(packet_id, "escalated",
                            expected=row,
                            reason=f"approved and green but the merge was refused: {exc}")
            return
        self.say(f"{packet_id}: merged as {commit[:12]}")
        self.transition(
            packet_id,
            "merged",
            expected=row,
            kind="auto_merged",
            merge_commit=commit,
        )

    def send_back(
        self,
        packet_id: str,
        row,
        feedback: str,
        *,
        source: str,
        review_verdict: dict | None = None,
    ) -> None:
        """Return a packet to the builder, or block it if it has had enough."""
        cycles = row["rework_cycles"] + 1
        cap = self.config["breakers"]["max_rework_cycles"]
        if self.dry:
            self.would(f"{packet_id} -> rework (cycle {cycles}, from {source})")
            return
        with store.write(self.conn):
            fresh = store.get(self.conn, packet_id)
            if (
                fresh["status"] != row["status"]
                or fresh["version"] != row["version"]
            ):
                self.say(f"{packet_id}: stale rework transition ignored")
                return
            if review_verdict is not None:
                store.record(
                    self.conn,
                    packet_id,
                    "review_verdict",
                    **review_verdict,
                )
            store.record(self.conn, packet_id, "feedback", source=source, text=feedback)
            store.set_fields(self.conn, packet_id, rework_cycles=cycles)
            if cycles >= cap:
                store.set_status(self.conn, packet_id, "blocked",
                                 reason=f"{cycles} rework cycles, cap is {cap}. "
                                        f"Last source: {source}.")
            else:
                store.set_status(self.conn, packet_id, "rework", kind=f"rework_{source}")

    # --- 5. dispatch ---------------------------------------------------------

    def selectable(self) -> list:
        rows = {r["id"]: r for r in store.all_packets(self.conn)}
        merged = {i for i, r in rows.items() if r["status"] == "merged"}
        in_flight = sum(1 for r in rows.values()
                        if r["status"] in ("building", "awaiting_ci", "review"))
        room = self.config["max_concurrent_packets"] - in_flight
        if room <= 0:
            return []
        ready = []
        for packet in P.load_board(self.board_dir):
            row = rows.get(packet.id)
            if row is None or row["status"] not in store.DISPATCHABLE:
                continue
            if not all(d in merged for d in packet.meta["depends_on"]):
                continue
            if not packet.meta["acceptance_tests"]:
                # No named test means no definition of done. The controller
                # will not dispatch work it cannot judge, and it will not
                # invent a criterion to make the packet dispatchable.
                self.transition(
                    packet.id,
                    "escalated",
                    expected=row,
                    reason="packet names no acceptance tests, so it has no "
                    "definition of done. Slice it properly first.",
                )
                continue
            ready.append((packet, row))
        # rework before queued: work already under review is closer to done
        # than work that has not started, and holding it costs a review slot.
        ready.sort(key=lambda pair: (pair[1]["status"] != "rework", pair[0].id))
        return ready[:room]

    def dispatch(self, packet: P.Packet, row) -> None:
        packet_id = packet.id
        attempt = row["attempts"] + 1
        cap = self.config["breakers"]["max_attempts"]
        if attempt > cap:
            self.transition(packet_id, "blocked",
                            reason=f"{row['attempts']} attempts, cap is {cap}")
            return

        if row["base_sha"]:
            base_sha = row["base_sha"]
        elif self.dry:
            # A dry run may read the existing remote-tracking ref but must not
            # fetch and update it.
            base_sha = forge.git(["rev-parse", "origin/main"], REPO)
        else:
            base_sha = forge.resolve_base(
                REPO, token_env=self._token("builder")
            )
        worktree = self.worktree_for(packet_id)
        run_dir = self.run_dir(packet_id, attempt)
        oracle = P.load_oracle()
        feedback = self.latest_feedback(packet_id)
        prompt = agents.builder_prompt(
            packet, str(packet.path.relative_to(REPO)),
            [t for t in packet.meta["acceptance_tests"]], base_sha, feedback)

        if self.dry:
            self.would(f"acquire lease on {packet_id} (ttl "
                       f"{self.config['breakers']['lease_seconds']}s)")
            self.would(f"ensure worktree {worktree.relative_to(REPO)} on {packet.meta['branch']} "
                       f"at {base_sha[:12]}")
            self.would(f"run builder, attempt {attempt}, timeout "
                       f"{self.config['breakers']['per_attempt_wall_seconds']}s")
            self.would(f"verify {len(oracle)} pinned oracle hashes, worktree clean, "
                       f">=1 commit ahead of {base_sha[:12]}")
            self.would("fast gate, then push, then ensure PR, then -> awaiting_ci")
            self.say(f"  prompt ({len(prompt)} bytes) would be written to "
                     f"{run_dir.relative_to(REPO)}/prompt.txt")
            return

        with store.write(self.conn):
            if not store.acquire(self.conn, packet_id, self.owner,
                                 self.config["breakers"]["lease_seconds"]):
                self.say(f"{packet_id}: another controller holds the lease, skipping")
                return
            attempt_id = store.start_attempt(self.conn, packet_id, attempt,
                                             "rework" if row["status"] == "rework" else "build",
                                             base_sha)
            store.set_status(self.conn, packet_id, "building", kind="dispatched",
                             attempt=attempt, base_sha=base_sha)

        try:
            self.build(packet, row, attempt, attempt_id, base_sha, worktree, run_dir,
                       oracle, prompt)
        finally:
            with store.write(self.conn):
                store.release(self.conn, packet_id, self.owner)

    def build(self, packet, row, attempt, attempt_id, base_sha, worktree, run_dir,
              oracle, prompt) -> None:
        packet_id = packet.id
        try:
            action = forge.ensure_worktree(REPO, worktree, packet.meta["branch"], base_sha)
            forge.verify_repo_identity(
                worktree, self.config["github"]["repository"]
            )
        except forge.ForgeError as exc:
            self.finish(packet_id, attempt_id, "infra_error", str(exc))
            return
        if attempt == 1 and action == "reused":
            existing_head = forge.git(["rev-parse", "HEAD"], worktree)
            if existing_head != base_sha:
                self.finish(
                    packet_id,
                    attempt_id,
                    "infra_error",
                    "first attempt found a pre-existing branch with commits not owned by "
                    f"this ledger: HEAD {existing_head[:12]}, base {base_sha[:12]}",
                    hard=True,
                )
                return
        self.say(f"{packet_id}: worktree {action}, attempt {attempt}")

        if not self.renew_controller_lease():
            self.finish(
                packet_id,
                attempt_id,
                "controller_lease_lost",
                "global controller lease was lost before builder invocation",
                hard=True,
            )
            return
        agents.clear_result(worktree)
        result = agents.run_builder(self.config, worktree, run_dir, prompt,
                                    self.config["breakers"]["per_attempt_wall_seconds"])

        if result["outcome"] == "spawn_failed":
            self.finish(packet_id, attempt_id, "spawn_failed", result["detail"],
                        tokens=result["tokens"])
            return
        if result["outcome"] == "timeout":
            self.finish(packet_id, attempt_id, "timeout", result["detail"],
                        tokens=result["tokens"])
            return
        if result["outcome"] != "exited" or result.get("code") != 0:
            self.finish(
                packet_id,
                attempt_id,
                "builder_failed",
                f"builder exited with code {result.get('code')}",
                tokens=result["tokens"],
            )
            return
        if not self.renew_controller_lease():
            self.finish(
                packet_id,
                attempt_id,
                "controller_lease_lost",
                "global controller lease was lost while the builder was running",
                tokens=result["tokens"],
                hard=True,
            )
            return

        with store.write(self.conn):
            lease_renewed = store.renew(
                self.conn,
                packet_id,
                self.owner,
                self.config["breakers"]["lease_seconds"],
            )
        if not lease_renewed:
            self.finish(
                packet_id,
                attempt_id,
                "lease_lost",
                "packet lease was lost before validation",
                tokens=result["tokens"],
                hard=True,
            )
            return

        try:
            builder_result = agents.read_result(worktree)
        except agents.AgentError as exc:
            self.finish(packet_id, attempt_id, "bad_result", str(exc), tokens=result["tokens"])
            return

        if builder_result is None:
            self.finish(
                packet_id,
                attempt_id,
                "bad_result",
                "builder returned no .loop/result.json",
                tokens=result["tokens"],
            )
            return

        if builder_result and builder_result["outcome"] == "escalated":
            escalation = builder_result["escalation"]
            with store.write(self.conn):
                store.finish_attempt(self.conn, attempt_id, "escalated",
                                     tokens=result["tokens"], detail=json.dumps(escalation))
                store.set_status(
                    self.conn,
                    packet_id,
                    "escalated",
                    kind="builder_escalated",
                    reason=(
                        f"{escalation['reason']} "
                        f"(cites {escalation['spec_clause']})"
                    ),
                    escalation=escalation,
                )
            self.say(f"{packet_id}: builder escalated — {escalation['reason'][:120]}")
            return
        if builder_result["outcome"] != "built":
            self.finish(
                packet_id,
                attempt_id,
                builder_result["outcome"],
                builder_result["summary"] or "builder did not complete the packet",
                tokens=result["tokens"],
            )
            return

        # --- the checks that make a green PR mean something ------------------

        try:
            forge.verify_repo_identity(
                worktree, self.config["github"]["repository"]
            )
        except forge.ForgeError as exc:
            self.finish(
                packet_id,
                attempt_id,
                "git_trust_changed",
                str(exc),
                tokens=result["tokens"],
                hard=True,
            )
            return

        violations = P.oracle_violations(worktree, oracle)
        if violations:
            self.finish(packet_id, attempt_id, "oracle_violation",
                        f"pinned acceptance files changed: {violations}. No PR opened.",
                        tokens=result["tokens"], hard=True)
            return

        clean, dirt = forge.is_clean(worktree)
        if not clean:
            self.finish(packet_id, attempt_id, "dirty_worktree",
                        f"builder left uncommitted changes:\n{dirt[:1000]}",
                        tokens=result["tokens"])
            return

        ahead = forge.commits_ahead(worktree, base_sha)
        if ahead == 0:
            detail = (builder_result or {}).get("summary", "no result file written")
            self.finish(packet_id, attempt_id, "no_commits",
                        f"nothing committed against {base_sha[:12]}. Builder said: {detail[:400]}",
                        tokens=result["tokens"])
            return

        gate = gates.fast_gate(worktree, packet.meta["acceptance_tests"], self.config)
        if not gate["passed"]:
            self.finish(packet_id, attempt_id, "gate_failed",
                        f"failed at: {gate['step']}\n\n{gate['output']}",
                        tokens=result["tokens"])
            return

        # Tests and linters are processes too. Re-establish every trust
        # invariant after they run, immediately before privileged publication.
        try:
            forge.verify_repo_identity(
                worktree, self.config["github"]["repository"]
            )
        except forge.ForgeError as exc:
            self.finish(
                packet_id,
                attempt_id,
                "git_trust_changed",
                f"local gate changed git trust state: {exc}",
                tokens=result["tokens"],
                hard=True,
            )
            return
        violations = P.oracle_violations(worktree, oracle)
        if violations:
            self.finish(
                packet_id,
                attempt_id,
                "oracle_violation",
                f"local gate changed pinned acceptance files: {violations}. No PR opened.",
                tokens=result["tokens"],
                hard=True,
            )
            return
        clean, dirt = forge.is_clean(worktree)
        if not clean:
            self.finish(
                packet_id,
                attempt_id,
                "gate_dirtied_worktree",
                f"local gate left tracked or unignored changes:\n{dirt[:1000]}",
                tokens=result["tokens"],
            )
            return

        changed = forge.changed_paths(worktree, base_sha)
        governance = P.matches(
            changed, self.config["controller_protected_paths"]
        )
        if governance:
            self.finish(
                packet_id,
                attempt_id,
                "governance_violation",
                f"builder modified controller-owned paths: {sorted(set(governance))}",
                tokens=result["tokens"],
                hard=True,
            )
            return
        hits = P.in_blast_radius(changed)
        effective_blast_radius = bool(
            row["effective_blast_radius"] or packet.meta["blast_radius"] or hits
        )
        if hits and not row["effective_blast_radius"]:
            self.say(f"{packet_id}: declared routine but touched {sorted(set(hits))[:3]} — "
                     f"treating as blast radius")
            with store.write(self.conn):
                store.record(
                    self.conn,
                    packet_id,
                    "blast_radius_corrected",
                    paths=sorted(set(hits)),
                )
                store.set_fields(
                    self.conn,
                    packet_id,
                    effective_blast_radius=1,
                )
        packet.meta["blast_radius"] = effective_blast_radius

        try:
            head = forge.push(worktree, packet.meta["branch"], token_env=self._token("builder"))
            pr = forge.ensure_pr(worktree, packet.meta["branch"],
                                 f"{packet_id}: {packet.meta['title']}",
                                 self.pr_body(packet, base_sha),
                                 token_env=self._token("builder"))
            pr_identity = forge.pr_state(
                REPO, pr, token_env=self._token("builder")
            )
            forge.verify_pr_identity(
                pr_identity,
                expected_head_sha=head,
                expected_head_ref=packet.meta["branch"],
            )
        except forge.ForgeError as exc:
            self.finish(packet_id, attempt_id, "publish_failed", str(exc),
                        tokens=result["tokens"])
            return

        with store.write(self.conn):
            store.finish_attempt(self.conn, attempt_id, "published", tokens=result["tokens"],
                                 detail=f"{ahead} commit(s), head {head[:12]}, PR #{pr}")
            store.set_fields(
                self.conn,
                packet_id,
                pr_number=pr,
                head_sha=head,
                effective_blast_radius=int(effective_blast_radius),
            )
            store.set_status(self.conn, packet_id, "awaiting_ci", kind="published",
                             pr=pr, head=head)
        self.say(f"{packet_id}: PR #{pr} opened, {ahead} commit(s) -> awaiting_ci")

    def finish(self, packet_id: str, attempt_id: int, outcome: str, detail: str,
               *, tokens=None, hard: bool = False) -> None:
        """Record a failed attempt and decide whether the packet gets another.

        `hard=True` skips the retry: an oracle violation is not something a
        second attempt fixes, it is a fact the owner needs.
        """
        row = store.get(self.conn, packet_id)
        cap = self.config["breakers"]["max_attempts"]
        with store.write(self.conn):
            store.finish_attempt(self.conn, attempt_id, outcome, tokens=tokens, detail=detail)
            store.record(self.conn, packet_id, "feedback", source=outcome,
                         text=f"YOUR PREVIOUS ATTEMPT FAILED: {outcome}\n\n{detail}")
            if hard:
                store.set_status(self.conn, packet_id, "escalated", kind=outcome, reason=detail)
            elif row["attempts"] >= cap:
                store.set_status(self.conn, packet_id, "blocked", kind=outcome,
                                 reason=f"{row['attempts']} attempts, cap is {cap}. "
                                        f"Last failure: {outcome}. {detail[:400]}")
            else:
                store.set_status(self.conn, packet_id, "queued", kind=outcome, reason=None)
        self.say(f"{packet_id}: attempt {row['attempts']} {outcome}")

    def latest_feedback(self, packet_id: str) -> str:
        rows = self.conn.execute(
            "SELECT payload FROM events WHERE packet_id=? AND kind='feedback'"
            " ORDER BY seq DESC LIMIT 1", (packet_id,)).fetchone()
        return json.loads(rows["payload"]).get("text", "") if rows else ""

    def pr_body(self, packet: P.Packet, base_sha: str) -> str:
        return (
            f"Built by the autonomous loop from `{packet.path.relative_to(REPO)}`.\n\n"
            f"- packet: `{packet.id}`\n"
            f"- spec: {packet.meta['prd_ref']}\n"
            f"- base: `{base_sha}`\n"
            f"- acceptance (pinned by hash in `loop/oracle.lock`): "
            f"{', '.join(packet.meta['acceptance_tests'])}\n"
            f"- blast radius: {packet.meta['blast_radius']}\n\n"
            "The record of why this was built and what the reviewer said is the "
            "controller ledger published on `automation/loop-state`, not this "
            "description.\n"
        )

    # --- 6. audit ------------------------------------------------------------

    def publish_audit(self, digest_text: str) -> None:
        if self.dry:
            if self.config.get("audit", {}).get("enabled"):
                self.would(
                    f"publish ledger snapshot to {self.config['audit']['branch']}"
                )
            return
        if not self.renew_controller_lease():
            return
        try:
            head = audit.publish(
                self.conn,
                self.config,
                digest_text,
                token_env=self._token("builder"),
            )
        except (forge.ForgeError, OSError, ValueError) as exc:
            # The local ledger remains authoritative and the next tick retries
            # publication. Never turn an audit transport failure into a false
            # packet transition.
            self.say(f"audit publication failed: {exc}")
            return
        if head:
            self.say(f"audit snapshot published at {head[:12]}")

    # --- the tick ------------------------------------------------------------

    def tick(self) -> int:
        self.say(f"=== loop tick {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                 f"{'(DRY RUN)' if self.dry else ''} ===")

        with self.controller_lease() as acquired:
            if not acquired:
                return 0
            if not self.dry:
                self.sync_board()

            self.say("-- reconcile --")
            self.reconcile()

            self.say("-- preflight and breakers --")
            problems = self.blockers()
            if problems:
                self.say("LOOP PAUSED — nothing dispatched:")
                for problem in problems:
                    self.say(f"  * {problem}")
                text = self.digest()
                self.publish_audit(text)
                return 0
            self.say("  clear")

            self.say("-- advance --")
            self.advance()

            self.say("-- dispatch --")
            ready = self.selectable()
            if not ready:
                self.say("  nothing dispatchable")
            for packet, row in ready:
                self.say(f"  {packet.id} ({row['status']})")
                self.dispatch(packet, row)

            text = self.digest()
            self.publish_audit(text)
            self.say("=== tick complete ===")
            return 0

    def digest(self) -> str:
        import digest as D
        text = D.render(self.conn, self.board_dir, self.config)
        if self.dry:
            self.would(f"write loop/runs/digest.md ({len(text)} bytes)")
            return text
        target = REPO / "loop" / "runs" / "digest.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return text


# --- CLI ---------------------------------------------------------------------


@contextlib.contextmanager
def _ledger(dry_run: bool):
    """A connection to the ledger — or, for a dry run, to a throwaway copy.

    Every write in a dry run is guarded, but `store.init` still issues
    CREATE TABLE IF NOT EXISTS and SQLite still touches the WAL, so pointing
    a dry run at the real file changes bytes on disk. "--dry-run touched
    nothing" has to be literally true or it is not worth printing.
    """
    if not dry_run:
        with store.connect() as conn:
            yield conn
        return
    with tempfile.TemporaryDirectory() as tmp:
        scratch = pathlib.Path(tmp) / "state.db"
        if store.DB_PATH.exists():
            source = store.open_db(store.DB_PATH)
            destination = store.open_db(scratch)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        with store.connect(scratch) as conn:
            yield conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="the loop controller")
    parser.add_argument("command", choices=["tick", "status", "reconcile", "preflight",
                                            "oracle", "recover"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--board", default=None)
    parser.add_argument("--update", action="store_true", help="oracle: rewrite the lock file")
    args = parser.parse_args(argv)

    config = P.load_config()
    # A dry run on a machine that has never run the loop must not leave a
    # ledger behind. Every write is guarded anyway, but creating the file is
    # itself a write, and "--dry-run touched nothing" has to be literally
    # true or it is not worth printing.
    with _ledger(args.dry_run) as conn:
        store.init(conn)
        controller = Controller(config, conn, dry_run=args.dry_run, board_dir=args.board)

        if args.command == "tick":
            return controller.tick()
        if args.command == "reconcile":
            with controller.controller_lease() as acquired:
                if not acquired:
                    return 1
                controller.reconcile()
            return 0
        if args.command == "preflight":
            problems = gates.preflight(config)
            for problem in problems:
                print(f"  * {problem}")
            print("preflight clear" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        if args.command == "oracle":
            board = P.load_board(controller.board_dir)
            files = P.build_oracle(board, REPO)
            if args.update:
                P.write_oracle(files)
                print(f"pinned {len(files)} acceptance file(s) in loop/oracle.lock")
            else:
                current = P.load_oracle()
                drift = {k: v for k, v in files.items() if current.get(k) != v}
                missing = set(current) - set(files)
                for path in sorted(drift):
                    print(f"  CHANGED {path}")
                for path in sorted(missing):
                    print(f"  NO LONGER NAMED {path}")
                print("oracle matches the board" if not (drift or missing)
                      else f"{len(drift) + len(missing)} difference(s)")
                return 1 if (drift or missing) else 0
            return 0
        if args.command == "status":
            # Registering board packets the ledger has not seen is idempotent
            # and bootstraps from each packet's committed history, so
            # `status` on a fresh ledger shows the board rather than nothing.
            with controller.controller_lease() as acquired:
                if not acquired:
                    return 1
                controller.sync_board()
            for row in store.all_packets(conn):
                lease = " [leased]" if row["lease_owner"] else ""
                pr = f" PR#{row['pr_number']}" if row["pr_number"] else ""
                print(f"  {row['id']:<16} {row['status']:<12} "
                      f"attempts={row['attempts']} rework={row['rework_cycles']}{pr}{lease}")
            return 0
        if args.command == "recover":
            if not store.all_packets(conn):
                audit.restore(conn, config, token_env=controller._token("builder"))
                print("ledger restored from the audit branch")
            with controller.controller_lease() as acquired:
                if not acquired:
                    return 1
                controller.reconcile()
                text = controller.digest()
                controller.publish_audit(text)
            print("ledger reconciled and audit snapshot refreshed")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
