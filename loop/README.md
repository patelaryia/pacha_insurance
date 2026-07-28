# The autonomous build loop

Claude slices PRDs into packets and reviews finished work. Codex builds
packets. A **controller** owns every state transition. The owner merges, or
— once two GitHub identities exist — approves the design that lets the loop
merge routine work itself.

## The one idea

**Agents return evidence. The controller decides.**

Nothing an agent writes to a packet file, a status field, or a commit
message moves anything. The builder writes `.loop/result.json` inside its
own worktree; the reviewer has read-only tools and returns a JSON-schema
verdict on stdout. Both are schema-validated recommendations and are
rejected outright if malformed rather than defaulted.

State lives in `loop/state.db` — SQLite, WAL, leases, append-only events.
The controller never commits runtime state into local main. It publishes a
complete recovery snapshot, append-only event stream and digest to the
controller-only `automation/loop-state` branch. `controller.py recover`
restores an empty local ledger from that branch and reconciles GitHub.

---

## The state machine

```
                    ┌──────────────────────────── crash: lease expires ──┐
                    ▼                                                    │
   queued ──dispatch──▶ building ──┬── escalated (builder cites a spec clause)
      ▲                            ├── oracle violated ──▶ escalated
      │                            ├── dirty / no commits / gate red / timeout
      │                            │        └──▶ queued (retry) or blocked (cap)
      │                            └── green ──▶ push ──▶ PR ──▶ awaiting_ci
      │                                                              │
      │                              ┌──── CI red (+ failing logs) ──┤
      │                              ▼                               ▼
   rework ◀──── reviewer: rework ──── review ◀──────────── CI green ─┘
      │                              │
      │                              ├── approve + routine ──▶ merge_ready ──▶ merged
      │                              ├── approve + blast radius ──▶ escalated
      └──dispatch──▶ building        └── escalate ──▶ escalated
```

`rework` is **dispatchable**, on the same branch and the same PR, with the
reviewer's blocking findings — or the failing CI log — passed verbatim into
the next builder prompt. `merged` is only ever set by reconciling GitHub,
never asserted locally.

`advance` runs to a fixed point within one tick, so CI going green can carry
a packet through review and into rework and back to the builder in a single
run rather than three scheduled hours.

---

## The exact builder invocation

Verified against `codex --help` and `codex exec --help`, 2026-07-28:

```bash
codex exec --json \
  --output-last-message "$run_dir/last-message.txt" \
  --cd "$worktree" \
  --sandbox workspace-write \
  --skip-git-repo-check \
  - < "$run_dir/prompt.txt" > "$run_dir/events.jsonl" 2> "$run_dir/stderr.log"
```

Run through `subprocess.Popen` with `wait(timeout=…)`, so a timeout is a
classified outcome rather than a discarded exit code. `--dangerously-bypass-
approvals-and-sandbox` is never used.

The reviewer runs with `--permission-mode dontAsk`, a read-only tool allowlist,
no session persistence, and JSON-schema output on stdout. Builder and reviewer
subprocesses receive a minimal environment with GitHub and Pacha credentials
removed. Review happens in a clean detached worktree at the validated PR head,
not in the builder worktree or the primary checkout containing `loop/runs/`.

---

## File tree

```
loop/
  README.md            this file
  config.yml           every policy knob; a missing value is an error, not a default
  blast-radius.yml     owner-edited only
  oracle.lock          the definition of done, pinned by content hash
  controller.py        THE STATE MACHINE. Every transition is here
  store.py             SQLite ledger: leases, events, attempts, budget fold
  packets.py           packet spec, bootstrap state, oracle, blast radius
  forge.py             git + GitHub, every result verified rather than assumed
  gates.py             preflight and the fast local pre-filter
  agents.py            agent invocation, result schemas, prompts
  audit.py             publishes/restores the controller-only audit branch
  digest.py            renders the human-readable ledger digest
  drift.sh             weekly architecture drift job (report only)
  slice.md review.md drift.md    the three Claude commands
  hooks/pre-commit     hash-based, installed into each worktree
  test_controller.py   end-to-end and adversarial state-machine tests
  state.db             the ledger (gitignored)
  runs/                raw agent output + local digest (gitignored)

docs/packets/          THE BOARD — immutable spec + one-time bootstrap state
.claude/skills/        code-standards, review-criteria, packet-format
.codex/skills/         symlinks to those. One copy, two readers
.claude/commands/      symlinks to loop/{slice,review,drift}.md
```

---

## Install the schedule

```cron
REPO=/Users/aryiapatel/Documents/Research/Insurance\ TPA/pacha_insurance
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/aryiapatel/.local/bin
LOOP_PYTHON=/path/to/venv/bin/python

# One tick does everything: reconcile, advance, dispatch. Ticking often is
# cheap when there is nothing to do and is what makes CI polling work.
*/15 * * * *  cd "$REPO" && ./loop/controller.py tick >> loop/runs/tick.log 2>&1

# Architecture drift. Report only; never opens a PR.
0 7 * * 1     cd "$REPO" && ./loop/drift.sh >> loop/runs/drift.log 2>&1
```

There is one scheduled job now, not three. A renewable global lease owns the
whole tick — including reviewer invocation, approval, merge and audit
publication — so an overlapping run skips before any external action.

macOS sleeps and cron does not catch up. A missed tick is harmless — the
next one reconciles.

---

## Before the first real run

```bash
python3 loop/controller.py preflight
```

Currently **fails on this machine**, correctly:

```
* python3 is older than 3.12, which pyproject.toml requires. A gate that
  passes under the wrong interpreter is not a gate. Set LOOP_PYTHON.
```

Preflight also checks: ruff importable or on PATH, project dependencies
importable, `git`/`gh`/`codex`/`claude` present, `origin/main` resolvable,
the exact configured GitHub repository, and credential identities plus
repository access. It also refuses to execute while controller, governance or
packet-spec paths are uncommitted. **Any failure pauses the whole loop** rather
than failing packets one at a time.

Then pin the oracle and have the result reviewed:

```bash
python3 loop/controller.py oracle --update
```

---

## Running it by hand

```bash
python3 loop/controller.py preflight        # can this machine build at all?
python3 loop/controller.py status           # every packet, status, lease, PR
python3 loop/controller.py oracle           # has the definition of done drifted?
python3 loop/controller.py tick --dry-run   # print every action, write nothing
python3 loop/controller.py reconcile        # GitHub truth -> ledger
python3 loop/controller.py recover          # restore audit snapshot + reconcile
python3 -m pytest loop/test_controller.py -q
```

Pause everything:

```bash
echo "re-slicing PRD-10" > loop/PAUSED
```

---

## Two identities, and what they unlock

`main protection` requires an approving review **and** a code-owner review.
One account authors every PR, and GitHub forbids approving your own — so
with a single identity the loop **cannot** legitimately merge. It stops at
`merge_ready` and prints the command.

To close that gap: install a GitHub App with contents+PR write, export its
installation token, and set

```yaml
github:
  builder_token_env: PACHA_BUILDER_TOKEN     # the App pushes and opens PRs
  reviewer_token_env: PACHA_REVIEWER_TOKEN   # your account approves
  auto_merge: true
```

Preflight refuses a half-configured identity. It also authenticates each
configured token, resolves it to a GitHub user or App actor, verifies access
to the configured repository, and rejects two different token values that
resolve to the same actor.

**The loop never passes `--admin`.** If the ruleset refuses a merge, that
refusal is information and the packet escalates.

Even fully configured, `blast_radius: true` still escalates on approval.
That is not a limitation to remove later; it is the point.

---

## The rule that holds the whole thing up

**The builder cannot change the definition of done.** Enforced by content
hash, not by directory name, at three layers:

1. `.github/CODEOWNERS` — `tests/acceptance/` needs owner review to merge.
2. `loop/hooks/pre-commit` — re-hashes any staged file that appears in
   `oracle.lock`. Fast feedback; defeated by `--no-verify`.
3. **The controller** — re-hashes every pinned file in the worktree after
   the builder exits and refuses to push or open a PR if one moved. Records
   `oracle_violation` and escalates without a retry, because a second
   attempt does not fix that; it is a fact the owner needs.

Directory names were the previous design's boundary and were wrong in both
directions: the prompt forbade all of `tests/`, making ED-7's required unit
tests unsatisfiable, while the config protected only two directories, so a
named acceptance test under `tests/integration/` was editable. The hash list
is exact. **New unit and integration tests are explicitly encouraged** — the
builder is told to write the ones ED-7 requires.

---

## What a green PR means

The controller will not open a PR until, in this order:

1. every pinned oracle hash is unchanged in the worktree;
2. `git status --porcelain` is clean apart from `.loop/`;
3. `git rev-list --count $base..HEAD` is at least 1;
4. the fast local gate passes (ruff, the two invariant linters, the packet's
   named tests);
5. the push succeeded **and** `origin/<branch>` equals local `HEAD`;
6. `gh pr create` produced a PR number the controller then re-read.

Then the packet waits at `awaiting_ci` until all six required check
contexts report. An unreported required check is `pending`, never green.
The exact validated head SHA, head branch and `main` base are checked before
review, after review, immediately before approval and immediately before merge.

---

## Honest limits

- **Not zero-touch today.** `AGENTS.md:3` and `CLAUDE.md:3` both require
  owner approval, and the ruleset enforces it. The realistic target is: no
  intervention for routine, well-specified packets; interruption only for
  genuine spec ambiguity, a protected-test reissue, or declared blast
  radius. Closing the last gap is a governance change plus the second
  identity above, not more code.
- **Every packet on the board today is `blast_radius: true`.** Migrations,
  money, authz, egress, ICON and PII cover the whole current roadmap. The
  auto-merge lane is near-empty until the board contains genuinely routine work.
- **Token budgets are wall-clock.** `codex exec --json` token accounting is
  not a documented contract; counts are recorded when present and never
  enforced on. A partial token count that looks like a budget is worse than
  no budget.
- **The fast gate is not CI.** It is a pre-filter. GitHub's six checks are
  the oracle, deliberately, so the PostgreSQL tier is not paid twice on a
  laptop whose timings are not comparable anyway.
